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
        logger.info("插件 [群文件失效检查] 已加载 (最终调试版)。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        logger.info("--- on_group_message [步骤 1]: 函数被成功调用 ---")
        try:
            group_id = int(event.get_group_id())
            logger.info(f"--- on_group_message [步骤 2]: 成功获取 group_id: {group_id} ---")
            
            if self.group_whitelist and group_id not in self.group_whitelist:
                logger.info(f"--- on_group_message [步骤 2.1]: 群 {group_id} 不在白名单，已忽略。 ---")
                return

            # 【关键修改点】: 我们不再访问底层的 message_obj.message，而是使用框架推荐的 get_messages()
            # 它返回的是一个由 Comp.File, Comp.Plain 等对象组成的列表
            message_components = event.get_messages()
            logger.info(f"--- on_group_message [步骤 4]: 成功获取消息组件列表，共 {len(message_components)} 个组件 ---")

            logger.info("--- on_group_message [步骤 5]: 开始遍历消息组件... ---")
            file_found = False
            for segment in message_components:
                # 【关键修改点】: 直接判断 segment 是否是 Comp.File 的实例
                if isinstance(segment, Comp.File):
                    logger.info(f"--- [调试] 发现文件对象，其可用属性为: {dir(segment)} ---")
                    file_found = True
                    logger.info("--- on_group_message [步骤 6]: 找到文件组件，准备解析... ---")
                    
                    # 从 Comp.File 对象中直接获取属性
                    file_name = segment.name  # 文件名
                    file_id = segment.file_id     # 文件ID
                    
                    if file_name and file_id:
                        logger.info(f"--- on_group_message [步骤 6.1]: 成功从消息组件中解析到文件: '{file_name}', ID: {file_id} ---")
                        # 这里的第三个参数需要传入文件组件对象本身
                        asyncio.create_task(self._handle_file_check_flow(event, file_name, file_id, segment))
                    else:
                        logger.warning("--- on_group_message [步骤 6.2]: 找到了文件组件，但无法获取文件名或ID。 ---")
                    break # 通常一条消息只有一个文件，找到后即可退出循环
            
            if file_found:
                logger.info("--- on_group_message [步骤 7]: 消息组件遍历完成（已找到文件）。 ---")
            else:
                logger.info("--- on_group_message [步骤 7]: 消息组件遍历完成（未找到文件）。 ---")

        except Exception as e:
            logger.error(f"--- on_group_message [致命错误]: 函数在执行过程中崩溃! 原因: {e} ---", exc_info=True)

    def _get_file_component_from_event(self, event: AstrMessageEvent) -> Optional[Comp.File]:
        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                return segment
        return None
    
    # ... 后续所有功能函数 (_send_or_forward, _handle_file_check_flow, 等) 都保持不变 ...
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

    async def _handle_file_check_flow(self, event: AstrMessageEvent, file_name: str, file_id: str, file_component: Comp.File):
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        await asyncio.sleep(self.pre_check_delay_seconds)
        logger.info(f"[{group_id}] [阶段一] 开始即时检查: '{file_name}'")
        is_gfs_valid = await self._check_validity_via_gfs(event, file_id)
        if is_gfs_valid:
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
            logger.info(f"[{group_id}] 初步检查通过，已加入延时复核队列。")
            asyncio.create_task(self._task_delayed_recheck(event, file_name, file_id))
        else:
            logger.error(f"❌ [{group_id}] [阶段一] 文件 '{file_name}' 即时检查已失效!")
            try:
                preview_text, encoding, is_text = await self._get_text_preview(file_component)
                failure_message = f"⚠️ 您发送的文件「{file_name}」已失效（对其他群友可能无法下载）。"
                if is_text and preview_text:
                    preview_text_short = preview_text[:self.preview_length]
                    failure_message += f"\n机器人仍可获取到以下内容预览：\n{preview_text_short}"
                    if len(preview_text) > self.preview_length: failure_message += "..."
                await self._send_or_forward(event, failure_message, message_id)
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段一] 回复失效通知时再次发生错误: {send_e}")
            logger.info(f"[{group_id}] 初步检查失败，不进行延时复核。")
    
    async def _check_validity_via_gfs(self, event: AstrMessageEvent, file_id: str) -> bool:
        group_id = int(event.get_group_id())
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            url_result = await client.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
            return bool(url_result and url_result.get('url'))
        except Exception:
            return False

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
        is_still_valid = await self._check_validity_via_gfs(event, file_id)
        if not is_still_valid:
            logger.error(f"❌ [{group_id}] [阶段二] 文件 '{file_name}' 在延时复核时确认已失效!")
            try:
                failure_message = f"❌ 经 {self.check_delay_seconds} 秒后复核，您发送的文件「{file_name}」已失效。"
                await self._send_or_forward(event, failure_message, message_id)
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段二] 回复失效通知时再次发生错误: {send_e}")
        else:
             logger.info(f"✅ [{group_id}] [阶段二] 文件 '{file_name}' 延时复核通过，保持沉默。")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")