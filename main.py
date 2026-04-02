import time
import traceback
from typing import Dict, TypedDict, Optional
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.provider.entities import LLMResponse  

# 定义结构化数据类型
class SessionStats(TypedDict):
    prompt: int
    completion: int
    total: int
    count: int
    start_time: float

class SessionData(TypedDict):
    current: Optional[SessionStats]
    last_token_time: Optional[float]
    session_start: float
    last_active_time: float
    pending_auto: bool

@register("token_tracker", "Lystars", 
          "输入/token以查看对话段token统计信息，支持自动统计、自动重置和自动清理", 
          "1.2.0")
class TokenTracker(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 使用结构化类型
        self.stats: Dict[str, SessionData] = {}
        
        # 安全解析配置，带容错处理
        try:
            self.auto_interval_hours = self._safe_get_config_float(config, "interval_hours", 24.0, 1.0, 720.0)
        except ValueError as e:
            logger.error(f"配置解析失败: {e}，使用默认值24.0小时")
            self.auto_interval_hours = 24.0
        
        try:
            session_ttl_hours = self._safe_get_config_float(config, "session_ttl_hours", 72.0, 1.0, 720.0)
            self.session_ttl = session_ttl_hours * 60 * 60  # 转换为秒
        except ValueError as e:
            logger.error(f"配置解析失败: {e}，使用默认值72.0小时")
            self.session_ttl = 72.0 * 60 * 60
        
        # 清理性能优化：设置清理间隔（秒）
        self.cleanup_interval = 300  # 5分钟清理一次
        self.last_cleanup_time = time.monotonic()
        
        logger.info(f"TokenTracker插件已加载，自动统计间隔: {self.auto_interval_hours}小时，会话过期时间: {self.session_ttl/3600:.1f}小时")
    
    def _safe_get_config_float(self, config: AstrBotConfig, key: str, default: float, min_val: float, max_val: float) -> float:
        """安全获取配置浮点数，带范围验证"""
        try:
            value = config.get(key, default)
            if value is None:
                return default
            
            # 转换为浮点数
            float_value = float(value)
            
            # 验证范围
            if not (min_val <= float_value <= max_val):
                raise ValueError(f"配置项 '{key}' 的值 {float_value} 超出范围 [{min_val}, {max_val}]")
            
            return float_value
        except (ValueError, TypeError) as e:
            raise ValueError(f"配置项 '{key}' 解析失败: {e}")
    
    def _session_id(self, event: AstrMessageEvent) -> str:
        try:
            platform = getattr(event, 'platform_name', 'unknown')
            session_id = event.get_session_id()
            return f"{platform}_{session_id}"
        except Exception:
            return f"unknown_{id(event)}"
    
    def _init_session_stats(self, sid: str) -> SessionStats:
        """初始化或重置会话的当前统计"""
        if sid not in self.stats:
            self.stats[sid] = SessionData(
                current=None,
                last_token_time=None,
                session_start=time.monotonic(),
                last_active_time=time.monotonic(),  # 最后活跃时间
                pending_auto=False
            )
        
        # 初始化当前统计
        current_stats: SessionStats = {
            "prompt": 0, 
            "completion": 0, 
            "total": 0, 
            "count": 0,
            "start_time": time.monotonic()
        }
        self.stats[sid]["current"] = current_stats
        return current_stats
    
    def _get_current_stats(self, sid: str) -> SessionStats:
        """获取当前统计，如果不存在则初始化"""
        if sid not in self.stats or self.stats[sid]["current"] is None:
            return self._init_session_stats(sid)
        return self.stats[sid]["current"]  # type: ignore
    
    def _check_auto_token(self, sid: str) -> bool:
        """检查是否应该执行自动统计"""
        if sid not in self.stats:
            return False
        
        data = self.stats[sid]
        now = time.monotonic()  # 统一时间戳
        interval_seconds = self.auto_interval_hours * 60 * 60
        
        # 检查会话是否过期 - 基于最后活跃时间
        last_active = data["last_active_time"]
        if now - last_active > self.session_ttl:
            # 会话过期，清理
            del self.stats[sid]
            return False
        
        # 检查是否需要自动统计
        last_token_time = data["last_token_time"]
        session_start = data["session_start"]
        
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
        now = time.monotonic()
        
        if current_stats["count"] == 0:
            # 没有统计记录时也要清除状态
            if sid in self.stats:
                self.stats[sid]["pending_auto"] = False
                self.stats[sid]["last_token_time"] = now
                self.stats[sid]["last_active_time"] = now
            return
        
        # 计算距离上次统计的时间
        last_token_time = self.stats[sid]["last_token_time"]
        session_start = self.stats[sid]["session_start"]
        
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
        # 更新最后统计时间和活跃时间
        self.stats[sid]["last_token_time"] = now
        self.stats[sid]["last_active_time"] = now
        # 清除待处理标志
        self.stats[sid]["pending_auto"] = False
        
        logger.info(f"自动统计执行: {sid}, 消耗={current_stats['total']}tokens, 间隔={elapsed_hours:.1f}小时")
        
        # 使用event.send()发送消息
        await event.send(event.plain_result(auto_msg))
    
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            sid = self._session_id(event)
            now = time.monotonic()  # 统一时间戳
            
            # 更新最后活跃时间（无论是否有usage，用户发送消息就算活跃）
            if sid in self.stats:
                self.stats[sid]["last_active_time"] = now
            
            # 检查是否需要自动统计（在记录新token之前检查）
            should_auto = self._check_auto_token(sid)
            if should_auto:
                # 标记为待处理，将在本次回复后执行
                self.stats[sid]["pending_auto"] = True
            
            # 记录token使用（带空值保护）
            usage = resp.raw_completion.usage if resp.raw_completion else None
            if usage:
                stats = self._get_current_stats(sid)
                
                # 安全处理usage字段，避免None值
                stats["prompt"] += int(usage.prompt_tokens or 0)
                stats["completion"] += int(usage.completion_tokens or 0)
                stats["total"] += int(usage.total_tokens or 0)
                stats["count"] += 1
                
                logger.debug(f"记录token: {sid}, 本次={int(usage.total_tokens or 0)}, 累计={stats['total']}")
            
            # 性能优化：按间隔清理过期会话
            self._cleanup_expired_sessions()
            
            # 如果有待处理的自动统计，执行它
            # 注意：这里执行的是上一统计段的自动统计
            if sid in self.stats and self.stats[sid]["pending_auto"]:
                await self._execute_auto_token(event, sid)
                
        except Exception:
            # 详细的异常处理，包含堆栈信息
            logger.error(f"处理LLM响应出错: {traceback.format_exc()}")
    
    @filter.command("token")
    async def show_token(self, event: AstrMessageEvent):
        """显示当前对话段的token统计"""
        # 性能优化：按间隔清理过期会话
        self._cleanup_expired_sessions()
        
        sid = self._session_id(event)
        now = time.monotonic()
        
        # 更新最后活跃时间（用户使用命令也算活跃）
        if sid in self.stats:
            self.stats[sid]["last_active_time"] = now
            self.stats[sid]["last_token_time"] = now
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
        """清理过期会话 - 基于最后活跃时间，带性能优化"""
        now = time.monotonic()  # 统一时间戳
        
        # 性能优化：检查是否需要清理
        if now - self.last_cleanup_time < self.cleanup_interval:
            return 0  # 未到清理间隔
        
        self.last_cleanup_time = now
        expired_sids = []
        
        for sid, data in self.stats.items():
            # 使用最后活跃时间判断过期
            last_active = data["last_active_time"]
            if now - last_active > self.session_ttl:
                expired_sids.append(sid)
        
        for sid in expired_sids:
            del self.stats[sid]
            logger.debug(f"清理过期会话: {sid}")
        
        return len(expired_sids)
