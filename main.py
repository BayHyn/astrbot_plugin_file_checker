import asyncio
import os
from typing import List, Dict, Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
# --- 修正点: 统一使用 Comp 前缀 ---
import astrbot.api.message_components as Comp

@register(
    "astrbot_plugin_file_checker",
    "Foolllll",
    "群文件失效检查",
    "1.0.0",
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
        self.max_filesize_mb: int = self.config.get("max_filesize_mb", 100)
        
        self.download_semaphore = asyncio.Semaphore(5)
        
        logger.info("插件 [群文件失效检查] 已加载。")
        logger.info(f"成功时通知: {'开启' if self.notify_on_success else '关闭'}")
        logger.info(f"检查延时: {self.check_delay_seconds} 秒")
        logger.info(f"文件大小限制: {self.max_filesize_mb} MB (0为不限制)")
        
        if self.group_whitelist:
            logger.info(f"已启用群聊白名单，只在以下群组生效: {self.group_whitelist}")
        else:
            logger.info("未配置群聊白名单，插件将在所有群组生效。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                file_name = segment.name
                logger.info(f"检测到群 {group_id} 中的文件消息: '{file_name}'，已加入处理队列。")
                asyncio.create_task(self._handle_file_check(event, file_name, segment))
                break

    async def _handle_file_check(self, event: AstrMessageEvent, file_name: str, file_component: Comp.File):
        await asyncio.sleep(self.check_delay_seconds)
        
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        
        async with self.download_semaphore:
            logger.info(f"[{group_id}] 开始检查文件: '{file_name or '未知文件名'}' (获得处理许可)")
            local_file_path = None
            try:
                local_file_path = await file_component.get_file()
                
                file_size_bytes = os.path.getsize(local_file_path)
                actual_filename = os.path.basename(local_file_path)

                logger.info(f"[{group_id}] >> 文件已下载到本地: {local_file_path}")
                logger.info(f"[{group_id}] >> 实际文件名: {actual_filename}, 文件大小: {file_size_bytes} 字节")
                try:
                    with open(local_file_path, 'rb') as f:
                        head_content = f.read(200)
                        logger.info(f"[{group_id}] >> 文件头部内容 (前200字节): {head_content!r}")
                except Exception as read_e:
                    logger.error(f"[{group_id}] >> 读取文件头部内容失败: {read_e}")

                limit_bytes = self.max_filesize_mb * 1024 * 1024
                if self.max_filesize_mb > 0 and file_size_bytes > limit_bytes:
                    logger.info(f"文件 '{file_name or actual_filename}' (大小: {file_size_bytes / 1024 / 1024:.2f} MB) 超过 {self.max_filesize_mb} MB 的限制，检查终止。")
                    return 
                
                MINIMUM_VALID_SIZE_BYTES = 1024
                if file_size_bytes < MINIMUM_VALID_SIZE_BYTES:
                    raise ValueError(f"文件大小 ({file_size_bytes} B) 过小，判定为失效文件。")
                
                if not file_name:
                    file_name = actual_filename
                    logger.info(f"[{group_id}] 未能获取到原始文件名，使用临时文件名: {file_name}")
                
                logger.info(f"✅ [{group_id}] 文件 '{file_name}' 检查有效。")
                
                if self.notify_on_success:
                    try:
                        success_message = "✅ 您发送的文件检查有效，可以正常下载。"
                        # --- 修正点: 使用 Comp.Reply 和 Comp.Plain ---
                        chain = MessageChain([Comp.Reply(id=message_id), Comp.Plain(text=success_message)])
                        await event.send(chain)
                        logger.info(f"已向群 {group_id} 回复文件有效通知。")
                    except Exception as reply_e:
                        logger.error(f"向群 {group_id} 回复有效通知时发生错误: {reply_e}")
                
            except Exception as e:
                display_name = file_name or "未知文件"
                logger.error(f"❌ [{group_id}] 文件 '{display_name}' 经检查已失效! 原因: {e}")
                try:
                    failure_message = "❌ 您发送的文件经检查已失效或被服务器屏蔽。"
                    # --- 修正点: 使用 Comp.Reply 和 Comp.Plain ---
                    chain = MessageChain([Comp.Reply(id=message_id), Comp.Plain(text=failure_message)])
                    await event.send(chain)
                    logger.info(f"已向群 {group_id} 回复文件失效通知。")
                except Exception as send_e:
                    logger.error(f"向群 {group_id} 回复失效通知时再次发生错误: {send_e}")
            
            finally:
                if local_file_path and os.path.exists(local_file_path):
                    try:
                        os.remove(local_file_path)
                        logger.info(f"[{group_id}] 临时文件 '{local_file_path}' 已被删除。")
                    except OSError as e:
                        logger.warning(f"[{group_id}] 删除临时文件失败: {e}")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")
