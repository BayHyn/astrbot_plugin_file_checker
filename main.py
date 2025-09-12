import asyncio
import os
from typing import List, Dict, Optional
import datetime
import time

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
        logger.info("插件 [群文件失效检查] 已加载 (时间戳匹配复核模式)。")

    def _format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024: return f"{size_bytes} B"
        elif size_bytes < 1024**2: return f"{size_bytes/1024:.2f} KB"
        else: return f"{size_bytes/1024**2:.2f} MB"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                received_timestamp = time.time()
                
                logger.info(f"检测到群 {group_id} 中的文件消息，启动两阶段检查。")
                asyncio.create_task(self._task_immediate_preview(event, segment))
                asyncio.create_task(self._task_delayed_recheck(event, segment, received_timestamp))
                break

    async def _task_immediate_preview(self, event: AstrMessageEvent, file_component: Comp.File):
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        local_file_path = None
        
        async with self.download_semaphore:
            logger.info(f"[{group_id}] [阶段一] 开始即时检查...")
            try:
                local_file_path = await file_component.get_file()
                file_size_bytes = os.path.getsize(local_file_path)

                MINIMUM_VALID_SIZE_BYTES = 1024
                if file_size_bytes < MINIMUM_VALID_SIZE_BYTES:
                    raise ValueError(f"文件大小 ({self._format_size(file_size_bytes)}) 过小，判定为失效文件。")

                if self.notify_on_success:
                    is_text_file, decoded_text, encoding = self._identify_text_file(local_file_path)
                    success_message = "✅ 您发送的文件初步检查有效。"
                    if is_text_file and decoded_text:
                        preview_text = decoded_text[:200]
                        success_message += f"\n格式为 {encoding}，以下是预览：\n{preview_text}"
                        if len(decoded_text) > 200: success_message += "..."
                    chain = MessageChain([Comp.Reply(id=message_id), Comp.Plain(text=success_message)])
                    await event.send(chain)
                
            except Exception as e:
                logger.error(f"❌ [{group_id}] [阶段一] 文件即时检查已失效! 原因: {e}")
                try:
                    failure_message = f"❌ 您发送的文件经即时检查已失效或被服务器屏蔽。"
                    chain = MessageChain([Comp.Reply(id=message_id), Comp.Plain(text=failure_message)])
                    await event.send(chain)
                except Exception as send_e:
                    logger.error(f"[{group_id}] [阶段一] 回复失效通知时再次发生错误: {send_e}")
            finally:
                if local_file_path and os.path.exists(local_file_path):
                    try: os.remove(local_file_path)
                    except OSError: pass

    def _identify_text_file(self, file_path: str) -> tuple[bool, str, str]:
        try:
            with open(file_path, 'rb') as f:
                head_content_bytes = f.read(2048)
            detection_result = chardet.detect(head_content_bytes)
            encoding, confidence = detection_result['encoding'], detection_result['confidence']
            if encoding and confidence > 0.8:
                decoded_text = head_content_bytes.decode(encoding, errors='ignore').strip()
                return True, decoded_text, encoding
        except Exception:
            pass
        return False, "", "未知"

    async def _task_delayed_recheck(self, event: AstrMessageEvent, file_component: Comp.File, received_timestamp: float):
        await asyncio.sleep(self.check_delay_seconds)
        
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        
        logger.info(f"[{group_id}] [阶段二] 开始延时复核...")
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            
            api_result = await client.api.call_action('get_group_root_files', group_id=group_id)
            if not api_result or 'files' not in api_result:
                logger.warning(f"[{group_id}] [阶段二] 获取群文件列表失败或返回格式不正确。")
                return

            matched_file = None
            # --- 修改点: 将容差从 5 秒改为 3 秒 ---
            time_tolerance_seconds = 3
            for file_info in api_result['files']:
                modify_time = file_info.get('modify_time', 0)
                if abs(modify_time - received_timestamp) < time_tolerance_seconds:
                    matched_file = file_info
                    logger.info(f"[{group_id}] [阶段二] 成功通过时间戳匹配到文件: {matched_file.get('file_name')}")
                    break
            
            if not matched_file:
                logger.warning(f"[{group_id}] [阶段二] 未能在群文件列表中匹配到时间戳相近的文件。")
                return

            file_id = matched_file.get('file_id')
            file_name = matched_file.get('file_name', '未知文件名')
            logger.info(f"[{group_id}] [阶段二] 正在使用 file_id: {file_id} 获取下载链接...")
            
            url_result = await client.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
            
            if url_result and url_result.get('url'):
                logger.info(f"✅ [{group_id}] [阶段二] 文件 '{file_name}' 延时复核通过 (API调用成功)，保持沉默。")
            else:
                raise ValueError(f"get_group_file_url API 调用失败。响应: {url_result}")

        except Exception as e:
            logger.error(f"❌ [{group_id}] [阶段二] 文件在延时复核时确认已失效! 原因: {e}")
            try:
                failure_message = f"❌ 您发送的文件经 {self.check_delay_seconds} 秒后复核，已失效或被服务器屏蔽。"
                chain = MessageChain([Comp.Reply(id=message_id), Comp.Plain(text=failure_message)])
                await event.send(chain)
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段二] 回复失效通知时再次发生错误: {send_e}")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")