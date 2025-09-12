import asyncio
import os
from typing import List, Dict, Optional
import datetime
import time

# 导入 chardet 库，如果您的环境没有，请先执行 pip install chardet
import chardet

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Reply, Plain
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
        self.check_delay_seconds: int = self.config.get("check_delay_seconds", 30)
        self.download_semaphore = asyncio.Semaphore(5)
        logger.info("插件 [群文件失效检查] 已加载 (终极形态版)。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                logger.info(f"检测到群 {group_id} 中的文件消息，启动处理流程。")
                asyncio.create_task(self._handle_file_check_flow(event, segment))
                break

    async def _handle_file_check_flow(self, event: AstrMessageEvent, file_component: Comp.File):
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        
        # --- 新增：阶段一开始前，先等待3秒，确保文件同步 ---
        await asyncio.sleep(3)
        
        logger.info(f"[{group_id}] [阶段一] 开始即时检查...")
        
        check_result = await self._check_validity_via_gfs(event)
        
        if not check_result["is_valid"]:
            logger.error(f"❌ [{group_id}] [阶段一] 文件即时检查已失效! 原因: {check_result['reason']}")
            try:
                failure_message = "❌ 您发送的文件经即时检查已失效或无法在群文件中找到。"
                chain = MessageChain([Comp.Reply(id=message_id), Comp.Plain(text=failure_message)])
                await event.send(chain)
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段一] 回复失效通知时再次发生错误: {send_e}")
            return

        matched_file = check_result["matched_file"]
        file_name = matched_file.get('file_name', '未知文件名')
        file_id = matched_file.get('file_id')

        if self.notify_on_success:
            is_txt = file_name.lower().endswith('.txt')
            success_message = "✅ 您发送的文件初步检查有效。"
            if is_txt:
                preview_text, encoding = await self._get_text_preview(file_component)
                if preview_text:
                    success_message += f"\n格式为 {encoding}，以下是预览：\n{preview_text[:200]}"
                    if len(preview_text) > 200: success_message += "..."
            try:
                chain = MessageChain([Comp.Reply(id=message_id), Comp.Plain(text=success_message)])
                await event.send(chain)
                logger.info(f"[{group_id}] [阶段一] 已发送初步检查有效通知。")
            except Exception as reply_e:
                logger.error(f"向群 {group_id} 回复有效通知时发生错误: {reply_e}")
        
        logger.info(f"[{group_id}] 初步检查通过，已加入延时复核队列。")
        asyncio.create_task(self._task_delayed_recheck(event, file_name, file_id))

    async def _check_validity_via_gfs(self, event: AstrMessageEvent) -> dict:
        group_id = int(event.get_group_id())
        received_timestamp = time.time() - 3 # 减去我们刚刚等待的3秒，以匹配真实发送时间
        
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            
            api_result = await client.api.call_action('get_group_root_files', group_id=group_id)
            if not api_result or 'files' not in api_result:
                return {"is_valid": False, "reason": "获取群文件列表失败"}

            matched_file = None
            time_tolerance_seconds = 5
            for file_info in api_result['files']:
                if abs(file_info.get('modify_time', 0) - received_timestamp) < time_tolerance_seconds:
                    matched_file = file_info
                    break
            
            if not matched_file:
                return {"is_valid": False, "reason": "未能在群文件列表中匹配到文件"}

            file_id = matched_file.get('file_id')
            url_result = await client.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
            
            if url_result and url_result.get('url'):
                return {"is_valid": True, "matched_file": matched_file}
            else:
                return {"is_valid": False, "reason": f"get_group_file_url API 调用失败: {url_result}", "matched_file": matched_file}
        except Exception as e:
            return {"is_valid": False, "reason": f"检查过程中发生异常: {e}"}

    async def _get_text_preview(self, file_component: Comp.File) -> tuple[str, str]:
        local_file_path = None
        try:
            async with self.download_semaphore:
                local_file_path = await file_component.get_file()
                with open(local_file_path, 'rb') as f:
                    content_bytes = f.read(2048)
            detection = chardet.detect(content_bytes)
            encoding = detection.get('encoding', 'utf-8') or 'utf-8'
            decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
            return decoded_text, encoding
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
            
            if url_result and url_result.get('url'):
                logger.info(f"✅ [{group_id}] [阶段二] 文件 '{file_name}' 延时复核通过，保持沉默。")
            else:
                raise ValueError(f"get_group_file_url API 调用失败。响应: {url_result}")

        except Exception as e:
            logger.error(f"❌ [{group_id}] [阶段二] 文件在延时复核时确认已失效! 原因: {e}")
            try:
                failure_message = f"❌ 您发送的文件「{file_name}」经 {self.check_delay_seconds} 秒后复核，已失效或被服务器屏蔽。"
                chain = MessageChain([Comp.Reply(id=message_id), Comp.Plain(text=failure_message)])
                await event.send(chain)
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段二] 回复失效通知时再次发生错误: {send_e}")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")
