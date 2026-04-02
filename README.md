## Lystars
* 实时跟踪对话中的token消耗。
* 输入/token以查看当前对话段token统计信息，每次查看后自动重置。
* token统计信息与会话数据是相互独立的，重置会话并不影响当前的token统计。

## 安装方法：
* 直接在astrbot插件市场搜索插件名后自动安装
* 如安装失败，可尝试克隆源码：
```bash
# 克隆仓库到插件目录
cd /AstrBot/data/plugins
git clone https://github.com/Jiayu-Liu-0127/astrbot_plugin_token_tracker

# 控制台重启AstrBot
```
## 触发词：
/token
