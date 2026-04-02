import asyncio
import time
from typing import Dict, Optional
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.provider.entites import LLMResponse

@register("token_tracker", "Lystars", 
          "输入/token以查看对话段token统计信息，支持自动统计、自动重置和自动清理", 
          "1.1.1")
class TokenTracker(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 存储结构: {session_id: {"current": stats, "last_token_time": timestamp, "pending_auto": bool}}
        self.stats: Dict[str, Dict] = {}
        # 从配置获取参数
        self.config = config
        self.auto_interval_hours = float(self.config.get("interval_hours", 24.0))
        self.session_ttl = float(self.config.get("session_ttl_hours", 72.0)) * 60 * 60  # 转换为秒
        
        logger.info(f"TokenTracker插件已加载，自动统计间隔: {self.auto_interval_hours}小时，会话过期时间: {self.session_ttl/3600:.1f}小时")
    
    def _session_id(self, event: AstrMessageEvent) -> str:
        try:
            platform = getattr(event, 'platform_name', 'unknown')
            session_id = event.get_session_id()
            return f"{platform}_{session_id}"
        except Exception:
            return f"unknown_{id(event)}"
    
    def _init_session_stats(self, sid: str):
        """初始化或重置会话的当前统计"""
        if sid not in self.stats:
            self.stats[sid] = {
                "current": None, 
                "last_token_time": None,
                "session_start": time.monotonic(),
                "pending_auto": False  # 有待处理的自动统计
            }
        
        # 初始化当前统计
        self.stats[sid]["current"] = {
            "prompt": 0, 
            "completion": 0, 
            "total": 0, 
            "count": 0,
            "start_time": time.monotonic()
        }
        return self.stats[sid]["current"]
    
    def _get_current_stats(self, sid: str):
        """获取当前统计，如果不存在则初始化"""
        if sid not in self.stats or self.stats[sid]["current"] is None:
            return self._init_session_stats(sid)
        return self.stats[sid]["current"]
    
    def _check_auto_token(self, sid: str) -> bool:
        """检查是否应该执行自动统计"""
        if sid not in self.stats:
            return False
        
        data = self.stats[sid]
        now = time.monotonic()  # 统一时间戳
        interval_seconds = self.auto_interval_hours * 60 * 60
        
        # 检查会话是否过期
        if "current" in data and data["current"]:
            start_time = data["current"].get("start_time", 0)
            if now - start_time > self.session_ttl:
                # 会话过期，清理
                del self.stats[sid]
                return False
        
        # 检查是否需要自动统计
        last_token_time = data.get("last_token_time")
        session_start = data.get("session_start", 0)
        
        if last_token_time is None:
            # 从未使用过/token，从会话创建时间开始计算
            if session_start > 0 and now - session_start >= interval_seconds:
                return True
        elif now - last_token_time >= interval_seconds:
            return True
        
        return False
    
    async def _execute_auto_token(self, event: AstrMessageEvent, sid: str):
        """执行自动统计并发送消息"""
        if sid not in self.stats or self.stats[sid]["current"] is None:
            return
        
        current_stats = self.stats[sid]["current"]
        if current_stats["count"] == 0:
            return  # 没有统计记录时不输出
        
        # 计算距离上次统计的时间
        last_token_time = self.stats[sid].get("last_token_time")
        session_start = self.stats[sid].get("session_start", 0)
        now = time.monotonic()  # 统一时间戳
        
        if last_token_time is None:
            elapsed_hours = (now - session_start) / 3600
        else:
            elapsed_hours = (now - last_token_time) / 3600
        
        # 生成自动统计信息
        auto_msg = f"""⏰ 定时Token统计（已{elapsed_hours:.1f}小时未查看）：
• 请求次数：{current_stats['count']}次
• 输入Token：{current_stats['prompt']}个
• 输出Token：{current_stats['completion']}个
• 总计Token：{current_stats['total']}个

（统计已重置，下一轮定时统计将在{self.auto_interval_hours}小时后进行）"""
        
        # 重置当前统计
        self._init_session_stats(sid)
        # 更新最后统计时间
        self.stats[sid]["last_token_time"] = now
        # 清除待处理标志
        self.stats[sid]["pending_auto"] = False
        
        logger.info(f"自动统计执行: {sid}, 消耗={current_stats['total']}tokens, 间隔={elapsed_hours:.1f}小时")
        
        # 修复：使用event.send()而不是yield
        await event.send(event.plain_result(auto_msg))
    
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            sid = self._session_id(event)
            now = time.monotonic()  # 统一时间戳
            
            # 检查是否需要自动统计
            if self._check_auto_token(sid):
                # 标记为待处理，将在本次回复后执行
                self.stats[sid]["pending_auto"] = True
            
            # 记录token使用
            usage = resp.raw_completion.usage if resp.raw_completion else None
            if usage:
                stats = self._get_current_stats(sid)
                
                stats["prompt"] += usage.prompt_tokens
                stats["completion"] += usage.completion_tokens
                stats["total"] += usage.total_tokens
                stats["count"] += 1
                
                logger.debug(f"记录token: {sid}, 本次={usage.total_tokens}, 累计={stats['total']}")
            
            # 清理过期会话
            self._cleanup_expired_sessions()
            
            # 如果有待处理的自动统计，执行它
            # 修复：直接调用而不是async for循环
            if sid in self.stats and self.stats[sid].get("pending_auto", False):
                await self._execute_auto_token(event, sid)
                
        except Exception as e:
            logger.error(f"处理LLM响应出错: {e}")
    
    @filter.command("token")
    async def show_token(self, event: AstrMessageEvent):
        """显示当前对话段的token统计"""
        # 清理过期会话
        self._cleanup_expired_sessions()
        
        sid = self._session_id(event)
        
        # 更新最后手动统计时间
        if sid in self.stats:
            self.stats[sid]["last_token_time"] = time.monotonic()
            self.stats[sid]["pending_auto"] = False
        
        # 获取当前统计
        current_stats = self._get_current_stats(sid) if sid in self.stats else None
        
        if current_stats and current_stats["count"] > 0:
            msg = f"""📊 本段对话Token统计：
• 请求次数：{current_stats['count']}次
• 输入Token：{current_stats['prompt']}个
• 输出Token：{current_stats['completion']}个
• 总计Token：{current_stats['total']}个

（统计已重置，下一轮对话重新开始计数）"""
            
            # 重置当前统计
            self._init_session_stats(sid)
            
            logger.info(f"显示并重置统计: {sid}, 本段消耗={current_stats['total']}tokens")
        else:
            msg = "当前暂无Token消耗记录。继续对话以开始统计。"
            logger.debug(f"查询空统计: {sid}")
        
        yield event.plain_result(msg)
    

    
    def _cleanup_expired_sessions(self):
        """清理过期会话"""
        now = time.monotonic()  # 统一时间戳
        expired_sids = []
        
        for sid, data in self.stats.items():
            if "current" in data and data["current"]:
                start_time = data["current"].get("start_time", 0)
                if now - start_time > self.session_ttl:
                    expired_sids.append(sid)
        
        for sid in expired_sids:
            del self.stats[sid]
            logger.debug(f"清理过期会话: {sid}")
        
        return len(expired_sids)
