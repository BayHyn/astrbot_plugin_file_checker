import asyncio
import os
import json
from typing import List, Dict, Optional
import datetime
import httpx # 导入 httpx 库用于发送网络请求

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Reply, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

# =================================================================
# 这是一个用于进行“最终实验”的特殊版本 (V3 - 增加下载链接测试)
# =================================================================

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
        logger.info("插件 [群文件失效检查] 已加载 (最终实验模式 V3 - 增加下载链接测试)。")

    def _timestamp_to_str(self, timestamp: int) -> str:
        """将Unix时间戳转换为本地时区的可读字符串"""
        try:
            dt_object = datetime.datetime.fromtimestamp(timestamp)
            return dt_object.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return f"无法转换的时间戳: {timestamp}"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1)
    async def debug_print_file_component(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return
        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                received_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"--- 文件消息组件 __dict__ (接收时间: {received_time_str}) ---")
                logger.info(f"{segment.__dict__}")
                logger.info("--------------------")
                break

    @filter.command("testfiles")
    async def test_files_command(self, event: AstrMessageEvent):
        """(实验指令1) 获取群文件列表"""
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            group_id = int(event.get_group_id())
            
            payloads = {"group_id": group_id}
            logger.info(f"正在调用API: 'get_group_root_files'，参数: {payloads}")
            ret = await client.api.call_action('get_group_root_files', **payloads)
            
            if ret and 'files' in ret and isinstance(ret['files'], list):
                logger.info("--- get_group_root_files API 返回结果 (带时间转换) ---")
                for file_info in ret['files']:
                    if 'modify_time' in file_info:
                        file_info['modify_time_str'] = self._timestamp_to_str(file_info['modify_time'])
            
            logger.info(f"{json.dumps(ret, indent=2, ensure_ascii=False)}")
            logger.info("--------------------")

            yield event.plain_result("群文件列表API结果已打印到后台日志。")
        except Exception as e:
            logger.error(f"调用API失败: {e}", exc_info=True)
            yield event.plain_result(f"调用API失败: {e}")

    # --- 新增: 用于测试下载链接的指令 ---
    @filter.command("testdl")
    async def test_download_command(self, event: AstrMessageEvent, file_id: str):
        """(实验指令2) 根据 file_id 获取并测试下载链接"""
        try:
            yield event.plain_result(f"正在为 file_id: {file_id} 获取下载链接...")
            
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            group_id = int(event.get_group_id())
            
            # 1. 调用 get_group_file_url API
            payloads = {"group_id": group_id, "file_id": file_id}
            logger.info(f"正在调用API: 'get_group_file_url'，参数: {payloads}")
            ret = await client.api.call_action('get_group_file_url', **payloads)
            
            logger.info(f"--- get_group_file_url API 返回结果 ---\n{json.dumps(ret, indent=2, ensure_ascii=False)}\n--------------------")
            
            download_url = ret.get("url")
            if not download_url:
                yield event.plain_result(f"❌ 获取下载链接失败。API响应: {ret}")
                return
            
            yield event.plain_result(f"✅ 成功获取下载链接！正在用 HEAD 请求测试链接...")
            logger.info(f"获取到的下载链接: {download_url}")
            
            # 2. 使用 httpx 发送 HEAD 请求来测试链接
            async with httpx.AsyncClient(follow_redirects=True) as http_client:
                response = await http_client.head(download_url, timeout=20.0)
                
                logger.info(f"--- HEAD 请求响应 (Status: {response.status_code}) ---")
                for key, value in response.headers.items():
                    logger.info(f"{key}: {value}")
                logger.info("--------------------")

                content_length = response.headers.get('content-length')
                if 200 <= response.status_code < 300:
                    yield event.plain_result(f"✅ 链接测试成功！\n状态码: {response.status_code}\n文件大小: {self._format_size(int(content_length)) if content_length else '未知'}")
                else:
                    yield event.plain_result(f"❌ 链接测试失败！\n状态码: {response.status_code}")

        except Exception as e:
            logger.error(f"测试下载链接时出错: {e}", exc_info=True)
            yield event.plain_result(f"测试下载链接时出错: {e}")


    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")