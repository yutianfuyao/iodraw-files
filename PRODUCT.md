# Product

<!-- impeccable:product-schema 1 -->

## Platform

adaptive

## Users

Windows 玩家在本机运行游戏时，需要校准并控制一个基于截图的按键辅助工具。

## Product Purpose

提供一个本地桌面操作面板，用于配置两个屏幕识别区域、启动和停止辅助循环，并清楚显示游戏进程和脚本状态。

## Operating Context

工具只在 `yysls.exe` 正在运行且对应游戏窗口处于前台时派发按键。区域使用物理屏幕像素；玩家可能有多显示器或 Windows 缩放。

## Capabilities and Constraints

Python Windows 桌面程序。配置应能通过像素输入或截屏框选完成。脚本不读写、注入或修改游戏进程。攻击动作识别当前为运动检测原型。

## Evidence on Hand

现有实现、[DESIGN.md](DESIGN.md)、[config.json](config.json)。没有品牌资产或既定视觉系统。

## Product Principles

- 运行状态和安全阻断原因必须一眼可见。
- 配置流程优先减少坐标输入错误。
- 启动和停止必须可逆且立即生效。
- 自动化只在明确的游戏进程与前台窗口条件下有效。
