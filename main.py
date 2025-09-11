import asyncio
import os
from typing import List, Dict, Optional

import chardet

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Reply, Plain

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
        
        logger.info("插件 [群文件失效检查] 已加载。")
        logger.info(f"成功时通知: {'开启' if self.notify_on_success else '关闭'}")
        logger.info(f"复核延时: {self.check_delay_seconds} 秒")
        
        if self.group_whitelist:
            logger.info(f"已启用群聊白名单，只在以下群组生效: {self.group_whitelist}")
        else:
            logger.info("未配置群聊白名单，插件将在所有群组生效。")

    def _format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024: return f"{size_bytes} B"
        elif size_bytes < 1024**2: return f"{size_bytes/1024:.2f} KB"
        elif size_bytes < 1024**3: return f"{size_bytes/1024**2:.2f} MB"
        else: return f"{size_bytes/1024**3:.2f} GB"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                file_name = segment.name
                logger.info(f"检测到群 {group_id} 中的文件消息: '{file_name}'，启动两阶段检查任务。")
                asyncio.create_task(self._task_immediate_preview(event, file_name, segment))
                asyncio.create_task(self._task_delayed_recheck(event, file_name, segment))
                break

    async def _task_immediate_preview(self, event: AstrMessageEvent, file_name: str, file_component: Comp.File) -> bool:
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        local_file_path = None
        
        async with self.download_semaphore:
            logger.info(f"[{group_id}] [阶段一] 开始即时检查...")
            try:
                local_file_path = await file_component.get_file()
                
                file_size_bytes = os.path.getsize(local_file_path)
                display_filename = os.path.basename(local_file_path)
                logger.info(f"[{group_id}] [阶段一] >> 文件已下载到: {local_file_path}")
                logger.info(f"[{group_id}] [阶段一] >> 临时文件名: {display_filename}, 文件大小: {self._format_size(file_size_bytes)}")

                MINIMUM_VALID_SIZE_BYTES = 1024
                if file_size_bytes < MINIMUM_VALID_SIZE_BYTES:
                    raise ValueError(f"文件大小 ({self._format_size(file_size_bytes)}) 过小，判定为失效文件。")

                is_text_file, decoded_text, encoding = self._identify_text_file(local_file_path)
                
                if is_text_file:
                    logger.info(f"[{group_id}] [阶段一] >> chardet 识别为文本文件 ({encoding})。")
                    logger.info(f"[{group_id}] [阶段一] >> 文件头部内容 (解码后): {decoded_text[:200]}...")
                else:
                    logger.info(f"[{group_id}] [阶段一] >> chardet 识别为二进制文件。")

                if self.notify_on_success:
                    success_message = "✅ 您发送的文件初步检查有效，可以正常下载。"
                    if is_text_file and decoded_text:
                        preview_text = decoded_text[:200]
                        success_message += f"\n格式为 {encoding}，以下是预览：\n{preview_text}"
                        if len(decoded_text) > 200:
                            success_message += "..."
                    chain = MessageChain([Reply(id=message_id), Plain(text=success_message)])
                    await event.send(chain)
                    logger.info(f"[{group_id}] [阶段一] 已发送初步检查有效通知。")
                
                return True

            except Exception as e:
                logger.error(f"❌ [{group_id}] [阶段一] 文件即时检查已失效! 原因: {e}")
                try:
                    failure_message = f"❌ 您发送的文件经即时检查已失效或被服务器屏蔽。"
                    chain = MessageChain([Reply(id=message_id), Plain(text=failure_message)])
                    await event.send(chain)
                    logger.info(f"[{group_id}] [阶段一] 已发送即时检查失效通知。")
                except Exception as send_e:
                    logger.error(f"[{group_id}] [阶段一] 回复失效通知时再次发生错误: {send_e}")
                
                return False
            finally:
                if local_file_path and os.path.exists(local_file_path):
                    try: os.remove(local_file_path)
                    except OSError as e: logger.warning(f"[{group_id}] [阶段一] 删除临时文件失败: {e}")

    def _identify_text_file(self, file_path: str) -> tuple[bool, str, str]:
        try:
            with open(file_path, 'rb') as f:
                head_content_bytes = f.read(2048)
            detection_result = chardet.detect(head_content_bytes)
            encoding = detection_result['encoding']
            confidence = detection_result['confidence']
            if encoding and confidence > 0.8:
                decoded_text = head_content_bytes.decode(encoding, errors='ignore').strip()
                return True, decoded_text, encoding
        except Exception:
            pass
        return False, "", "未知"

    async def _task_delayed_recheck(self, event: AstrMessageEvent, file_name: str, file_component: Comp.File):
        await asyncio.sleep(self.check_delay_seconds)
        
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        local_file_path = None
        
        async with self.download_semaphore:
            logger.info(f"[{group_id}] [阶段二] 开始延时复核: '{file_name or '未知文件名'}'")
            try:
                local_file_path = await file_component.get_file()
                
                file_size_bytes = os.path.getsize(local_file_path)
                display_filename = os.path.basename(local_file_path)
                logger.info(f"[{group_id}] [阶段二] >> 文件已下载到: {local_file_path}")
                logger.info(f"[{group_id}] [阶段二] >> 临时文件名: {display_filename}, 文件大小: {self._format_size(file_size_bytes)}")

                MINIMUM_VALID_SIZE_BYTES = 1024
                if file_size_bytes < MINIMUM_VALID_SIZE_BYTES:
                    raise ValueError(f"文件大小 ({self._format_size(file_size_bytes)}) 过小，判定为失效文件。")
                
                logger.info(f"✅ [{group_id}] [阶段二] 文件延时复核通过，保持沉默。")

            except Exception as e:
                display_name = file_name or os.path.basename(local_file_path) if local_file_path else "未知文件"
                logger.error(f"❌ [{group_id}] [阶段二] 文件在延时复核时确认已失效! 原因: {e}")
                try:
                    failure_message = f"❌ 您发送的文件经 {self.check_delay_seconds} 秒后复核，已失效或被服务器屏蔽。"
                    chain = MessageChain([Reply(id=message_id), Plain(text=failure_message)])
                    await event.send(chain)
                    logger.info(f"[{group_id}] [阶段二] 已回复文件失效通知。")
                except Exception as send_e:
                    logger.error(f"[{group_id}] [阶段二] 回复失效通知时再次发生错误: {send_e}")
            
            finally:
                if local_file_path and os.path.exists(local_file_path):
                    try: os.remove(local_file_path)
                    except OSError as e: logger.warning(f"[{group_id}] [阶段二] 删除临时文件失败: {e}")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")