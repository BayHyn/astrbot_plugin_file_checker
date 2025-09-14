import asyncio
import os
from typing import List, Dict, Optional
import datetime
import time
import json

# 导入 chardet 库
import chardet

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Reply, Plain, Node
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register(
    "astrbot_plugin_file_checker",
    "Foolllll",
    "群文件失效检查",
    "1.1",
    "https://github.com/Foolllll-J/astrbot_plugin_file_checker"
)
class GroupFileCheckerPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.config = config if config else {}
        self.group_whitelist: List[int] = self.config.get("group_whitelist", [])
        self.group_whitelist = [int(gid) for gid in self.group_whitelist]
        self.notify_on_success: bool = self.config.get("notify_on_success", True)
        self.pre_check_delay_seconds: int = self.config.get("pre_check_delay_seconds", 3)
        self.check_delay_seconds: int = self.config.get("check_delay_seconds", 30)
        self.preview_length: int = self.config.get("preview_length", 200)
        self.forward_threshold: int = self.config.get("forward_threshold", 400)
        self.download_semaphore = asyncio.Semaphore(5)
        logger.info("插件 [群文件失效检查] 已加载。")

    async def _send_or_forward(self, event: AstrMessageEvent, text: str, message_id: int):
        if self.forward_threshold <= 0 or len(text) <= self.forward_threshold:
            chain = MessageChain([Reply(id=message_id), Plain(text=text)])
            await event.send(chain)
            return
        logger.info(f"[{event.get_group_id()}] 检测到长消息，将自动合并转发。")
        try:
            forward_node = Node(uin=event.get_self_id(), name="文件检查报告", content=[Reply(id=message_id), Plain(text=text)])
            await event.send(MessageChain([forward_node]))
        except Exception as e:
            logger.error(f"[{event.get_group_id()}] 合并转发长消息时出错: {e}", exc_info=True)
            fallback_text = text[:self.forward_threshold] + "... (消息过长且合并转发失败)"
            chain = MessageChain([Reply(id=message_id), Plain(text=fallback_text)])
            await event.send(chain)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        try:
            raw_message_chain = event.message_obj.raw_message
            if not isinstance(raw_message_chain, list): return

            for segment_data in raw_message_chain:
                if segment_data.get("type") == "file":
                    data = segment_data.get("data", {})
                    file_name = data.get("file")
                    file_component = self._get_file_component_from_event(event)
                    if file_name and file_component:
                        asyncio.create_task(self._handle_file_check_flow(event, file_name, file_component))
                    break
        except Exception as e:
            logger.error(f"解析原始消息时出错: {e}", exc_info=True)

    def _get_file_component_from_event(self, event: AstrMessageEvent) -> Optional[Comp.File]:
        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                return segment
        return None

    async def _handle_file_check_flow(self, event: AstrMessageEvent, file_name: str, file_component: Comp.File):
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        
        await asyncio.sleep(self.pre_check_delay_seconds)
        
        logger.info(f"[{group_id}] [阶段一] 开始即时检查: '{file_name}'")
        
        check_result = await self._verify_and_check_file(event, file_name)
        
        if check_result["is_valid"]:
            if self.notify_on_success:
                is_txt = file_name.lower().endswith('.txt')
                success_message = f"✅ 您发送的文件「{file_name}」初步检查有效。"
                if is_txt:
                    preview_text, encoding = await self._get_text_preview(file_component)
                    if preview_text:
                        preview_text_short = preview_text[:self.preview_length]
                        success_message += f"\n格式为 {encoding}，以下是预览：\n{preview_text_short}"
                        if len(preview_text) > self.preview_length: success_message += "..."
                await self._send_or_forward(event, success_message, message_id)

            verified_file_id = check_result["verified_file_id"]
            logger.info(f"[{group_id}] 初步检查通过，已加入延时复核队列。")
            asyncio.create_task(self._task_delayed_recheck(event, file_name, verified_file_id))

        else:
            logger.error(f"❌ [{group_id}] [阶段一] 文件 '{file_name}' 即时检查已失效! 原因: {check_result['reason']}")
            try:
                preview_text, encoding, is_text = await self._get_text_preview(file_component)
                failure_message = f"⚠️ 您发送的文件「{file_name}」已失效或无法在群文件中验证（对其他群友可能无法下载）。"
                if is_text and preview_text:
                    preview_text_short = preview_text[:self.preview_length]
                    failure_message += f"\n机器人仍可获取到以下内容预览：\n{preview_text_short}"
                    if len(preview_text) > self.preview_length: failure_message += "..."
                await self._send_or_forward(event, failure_message, message_id)
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段一] 回复失效通知时再次发生错误: {send_e}")
            logger.info(f"[{group_id}] 初步检查失败，不进行延时复核。")

    async def _verify_and_check_file(self, event: AstrMessageEvent, target_filename: str) -> dict:
        group_id = int(event.get_group_id())
        sender_id = int(event.get_sender_id())
        message_timestamp = event.message_obj.time
        
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            
            all_gfs_files = await self._get_all_gfs_files(client, group_id)
            if not all_gfs_files:
                return {"is_valid": False, "reason": "群文件系统为空或获取失败"}

            verified_file = None
            time_tolerance_seconds = 60
            for file_info in all_gfs_files:
                is_name_match = (file_info.get('file_name') == target_filename)
                is_uploader_match = (file_info.get('uploader') == sender_id)
                is_time_match = (abs(file_info.get('modify_time', 0) - message_timestamp) < time_tolerance_seconds)
                
                if is_name_match and is_uploader_match and is_time_match:
                    verified_file = file_info
                    logger.info(f"[{group_id}] [API查询] 三重验证成功匹配到文件: {target_filename}")
                    break
            
            if not verified_file:
                return {"is_valid": False, "reason": "三重验证失败，未能在群文件中匹配到该文件"}

            file_id = verified_file.get('file_id')
            url_result = await client.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
            
            if url_result and url_result.get('url'):
                return {"is_valid": True, "verified_file_id": file_id}
            else:
                return {"is_valid": False, "reason": f"get_group_file_url API 调用失败"}
        except Exception as e:
            return {"is_valid": False, "reason": f"验证过程中发生异常: {e}"}

    async def _get_all_gfs_files(self, client, group_id: int) -> List[Dict]:
        all_files = []
        root_api_result = await client.api.call_action('get_group_root_files', group_id=group_id)
        if not root_api_result: return []
        if root_api_result.get('files'): all_files.extend(root_api_result.get('files'))
        if root_api_result.get('folders'):
            for folder in root_api_result.get('folders', []):
                folder_id = folder.get('folder_id')
                if not folder_id: continue
                sub_api_result = await client.api.call_action('get_group_files_by_folder', group_id=group_id, folder_id=folder_id)
                if sub_api_result and sub_api_result.get('files'):
                    all_files.extend(sub_api_result.get('files'))
        return all_files

    async def _get_text_preview(self, file_component: Comp.File) -> tuple[str, str]:
        local_file_path = None
        try:
            async with self.download_semaphore:
                local_file_path = await file_component.get_file()
                with open(local_file_path, 'rb') as f: content_bytes = f.read(2048)
            detection = chardet.detect(content_bytes)
            encoding = detection.get('encoding', 'utf-8') or 'utf-8'
            if encoding and detection['confidence'] > 0.8:
                decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
                return decoded_text, encoding
            return "", "未知"
        except Exception as e:
            logger.error(f"获取文本预览失败: {e}")
            return "", "未知"
        finally:
            if local_file_path and os.path.exists(local_file_path):
                try: os.remove(local_file_path)
                except OSError: pass

    async def _task_delayed_recheck(self, event: AstrMessageEvent, file_name: str, file_id: str):
        await asyncio.sleep(self.check_delay_seconds)
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        logger.info(f"[{group_id}] [阶段二] 开始延时复核: '{file_name}'")
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            url_result = await client.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
            if not (url_result and url_result.get('url')):
                raise ValueError(f"get_group_file_url API 调用失败。响应: {url_result}")
            logger.info(f"✅ [{group_id}] [阶段二] 文件 '{file_name}' 延时复核通过，保持沉默。")
        except Exception as e:
            logger.error(f"❌ [{group_id}] [阶段二] 文件在延时复核时确认已失效! 原因: {e}")
            try:
                failure_message = f"❌ 经 {self.check_delay_seconds} 秒后复核，您发送的文件「{file_name}」已失效。"
                await self._send_or_forward(event, failure_message, message_id)
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段二] 回复失效通知时再次发生错误: {send_e}")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")