import asyncio
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.provider.entites import LLMResponse

@register("token_tracker", "Lystars", "输入/token以查看当前对话段token统计信息，每次查看后自动重置", "1.0.0")
class TokenTracker(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 存储结构：{session_id: {"current": stats}}
        self.stats = {}
        logger.info("TokenTracker插件已加载 - 分段统计模式")

    def _session_id(self, event: AstrMessageEvent) -> str:
        try:
            platform = getattr(event, 'platform_name', 'unknown')
            session_id = event.get_session_id()
            return f"{platform}_{session_id}"
        except:
            return f"unknown_{id(event)}"

    def _init_session_stats(self, sid: str):
        """初始化或重置会话的当前统计"""
        if sid not in self.stats:
            self.stats[sid] = {"current": None}
        
        # 初始化当前统计
        self.stats[sid]["current"] = {
            "prompt": 0, 
            "completion": 0, 
            "total": 0, 
            "count": 0,
            "start_time": asyncio.get_event_loop().time()
        }
        return self.stats[sid]["current"]

    def _get_current_stats(self, sid: str):
        """获取当前统计，如果不存在则初始化"""
        if sid not in self.stats or self.stats[sid]["current"] is None:
            return self._init_session_stats(sid)
        return self.stats[sid]["current"]

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            usage = resp.raw_completion.usage if resp.raw_completion else None
            if not usage:
                return
                
            sid = self._session_id(event)
            stats = self._get_current_stats(sid)
            
            stats["prompt"] += usage.prompt_tokens
            stats["completion"] += usage.completion_tokens
            stats["total"] += usage.total_tokens
            stats["count"] += 1
            
            logger.debug(f"记录token: {sid}, 本次={usage.total_tokens}, 累计={stats['total']}")
        except Exception as e:
            logger.error(f"记录token出错: {e}")

    @filter.command("token")
    async def show_token(self, event: AstrMessageEvent):
        sid = self._session_id(event)
        
        # 获取当前统计
        current_stats = self._get_current_stats(sid) if sid in self.stats else None
        
        if current_stats and current_stats["count"] > 0:
            # 生成统计信息
            msg = f"""📊 本段对话Token统计：
• 请求次数：{current_stats['count']}次
• 输入Token：{current_stats['prompt']}个
• 输出Token：{current_stats['completion']}个
• 总计Token：{current_stats['total']}个

（统计已重置，下一轮对话重新开始计数）"""
            
            # 重置当前统计（不清除会话）
            self._init_session_stats(sid)
            
            logger.info(f"显示并重置统计: {sid}, 本段消耗={current_stats['total']}tokens")
        else:
            msg = "当前暂无Token消耗记录。继续对话以开始统计。"
            logger.debug(f"查询空统计: {sid}")
        
        yield event.plain_result(msg)