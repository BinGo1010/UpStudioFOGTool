from __future__ import annotations

import ast
import html
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "presentation"
IMG = OUT / "images"
IMG.mkdir(parents=True, exist_ok=True)

CORE_FILES = ["main.py", "page1.py", "page2.py"]
COLORS = {
    "ink": "#17202A",
    "muted": "#5D6D7E",
    "line": "#B8C4D6",
    "blue": "#2F6BFF",
    "cyan": "#00A7C7",
    "green": "#10A66A",
    "orange": "#F59E0B",
    "red": "#E45757",
    "purple": "#7C3AED",
    "bg": "#F8FAFC",
}


def collect_metrics() -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    metrics: dict[str, dict[str, int]] = {}
    for name in CORE_FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        metrics[name] = {
            "lines": len(text.splitlines()),
            "loc": sum(
                1
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ),
            "classes": sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree)),
            "functions": sum(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in ast.walk(tree)
            ),
        }
    totals = {
        key: sum(item[key] for item in metrics.values())
        for key in ("lines", "loc", "classes", "functions")
    }
    return metrics, totals


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def wrap(value: str, max_chars: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for token in str(value).replace("\n", " / ").split():
        if len(line) + len(token) + (1 if line else 0) <= max_chars:
            line = f"{line} {token}".strip()
        else:
            if line:
                lines.append(line)
            line = token
    if line:
        lines.append(line)
    return lines or [""]


def text(
    x: float,
    y: float,
    value: object,
    size: int = 16,
    color: str | None = None,
    weight: str = "400",
    anchor: str = "start",
) -> str:
    return (
        f'<text x="{x}" y="{y}" font-family="Microsoft YaHei, SimHei, Arial" '
        f'font-size="{size}" fill="{color or COLORS["ink"]}" '
        f'font-weight="{weight}" text-anchor="{anchor}">{esc(value)}</text>'
    )


def multiline(
    x: float,
    y: float,
    lines: list[str],
    size: int = 14,
    color: str | None = None,
    weight: str = "400",
    anchor: str = "middle",
    gap: int = 18,
) -> str:
    return "\n".join(
        text(x, y + index * gap, line, size, color, weight, anchor)
        for index, line in enumerate(lines)
    )


def box(
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    subtitle: str = "",
    stroke: str | None = None,
    color: str | None = None,
) -> str:
    stroke = stroke or COLORS["line"]
    color = color or COLORS["ink"]
    body = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
        f'fill="white" stroke="{stroke}" stroke-width="1.5" filter="url(#shadow)"/>',
        multiline(x + w / 2, y + 32, wrap(title, max(8, int(w / 15))), 18, color, "800"),
    ]
    if subtitle:
        body.append(
            multiline(
                x + w / 2,
                y + 70,
                wrap(subtitle, max(10, int(w / 10)))[:3],
                13,
                COLORS["muted"],
                "400",
                "middle",
                16,
            )
        )
    return "\n".join(body)


def arrow(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        f'stroke="{COLORS["line"]}" stroke-width="2.2" marker-end="url(#arrow)"/>'
    )


def pill(x: float, y: float, w: float, h: float, value: str, color: str) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h / 2}" fill="{color}"/>'
        "\n"
        + text(x + w / 2, y + h / 2 + 6, value, 15, "white", "800", "middle")
    )


def write_svg(name: str, width: int, height: int, body: list[str]) -> None:
    content = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<defs>
<marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="{COLORS['line']}"/></marker>
<filter id="shadow" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="4" stdDeviation="4" flood-color="#CBD5E1" flood-opacity=".65"/></filter>
</defs>
<rect width="100%" height="100%" fill="{COLORS['bg']}"/>
{chr(10).join(body)}
</svg>
'''
    (IMG / name).write_text(content, encoding="utf-8")


def generate_svgs(metrics: dict[str, dict[str, int]], totals: dict[str, int]) -> None:
    body = [
        text(40, 48, "UpStudio FOG Tool 系统架构", 28, COLORS["ink"], "900"),
        text(40, 78, "多源硬件采集 + 同步记录 + 后处理标注导出", 16, COLORS["muted"]),
    ]
    devices = [
        ("4 路 USB Camera", "Qt Multimedia 预览/录制 camera1-4.mp4", COLORS["blue"]),
        ("Intel RealSense D435i", "RGB / Stereo / depth raw / frame timestamps", COLORS["cyan"]),
        ("5 个 WT IMU", "UDP 接收 9轴/角度/磁场/电量数据", COLORS["green"]),
        ("蓝牙遥控器", "单击 FOG，双击实验开始/结束", COLORS["orange"]),
    ]
    for index, (title, subtitle, color) in enumerate(devices):
        y = 125 + index * 105
        body.extend([box(45, y, 220, 72, title, subtitle, color, color), arrow(265, y + 36, 345, 265)])
    body.extend(
        [
            box(345, 205, 250, 135, "Page1 采集控制", "设备预检、实时预览、同步采集、事件记录、佩戴基线、角度置零", COLORS["purple"], COLORS["purple"]),
            arrow(595, 272, 670, 272),
            box(670, 168, 250, 210, "Session 数据包", "metadata / events / sync\nimu.csv\ncamera1-4.mp4\nD435i/RGB,Stereo,frames\nremote labels / baselines", COLORS["blue"], COLORS["blue"]),
            arrow(920, 272, 995, 272),
            box(995, 205, 250, 135, "Page2 标注导出", "6 路视频同步回放、FOG 区间精修、二/三分类标签、导出 labeled CSV", COLORS["red"], COLORS["red"]),
            pill(356, 390, 170, 34, "采集前设备预检", COLORS["purple"]),
            pill(710, 390, 170, 34, "统一时间零点", COLORS["blue"]),
            pill(1032, 390, 170, 34, "标签可追溯导出", COLORS["red"]),
        ]
    )
    write_svg("01_system_architecture.svg", 1280, 520, body)

    steps = [
        ("设备预检", "IMU / USB / D435i / 蓝牙遥控器在线检查；USB 可按通道选择不使用"),
        ("统一时间零点", "写入 recording_zero，所有 CSV 时间戳相对 session_start"),
        ("并行采集", "IMU UDP 队列写盘；USB QMediaRecorder；D435i 独立线程与磁盘队列"),
        ("遥控标注", "单击 FOG 起止；双击实验起止；实验开始自动保存佩戴基线"),
        ("同步账本", "session_events.csv + session_sync.csv 记录每路设备开始/停止事件"),
    ]
    body = [text(40, 48, "采集端工作流：从预检到可追溯数据落盘", 28, COLORS["ink"], "900")]
    for index, (title, subtitle) in enumerate(steps):
        x = 55 + index * 240
        body.append(box(x, 125, 200, 115, title, subtitle))
        if index < len(steps) - 1:
            body.append(arrow(x + 200, 182, x + 238, 182))
    lanes = [
        ("IMU 数据流", "5 路 WT IMU UDP → imu.csv\n实时表格 + 曲线预览", COLORS["green"]),
        ("视频数据流", "4 路 USB MP4 + D435i RGB/Stereo/depth\nUSB / D435i 分页浏览", COLORS["blue"]),
        ("事件数据流", "remote_fog_events / intervals\nexperiment_start/end 与 FOG start/end", COLORS["orange"]),
    ]
    for index, (title, subtitle, color) in enumerate(lanes):
        y = 310 + index * 78
        body.extend([box(110, y, 320, 56, title, "", color, color), multiline(470, y + 23, subtitle.split("\n"), 16, COLORS["ink"], "600", "start", 20), arrow(430, y + 28, 455, y + 28)])
    body.extend([box(850, 345, 310, 125, "最终 Session 文件夹", "原始数据、同步事件、标签事件、基线文件和 D435i 元数据集中保存", COLORS["purple"], COLORS["purple"]), arrow(760, 410, 850, 410)])
    write_svg("02_acquisition_pipeline.svg", 1280, 560, body)

    columns = [
        ("加载 Session", "自动加载当前/最近 session\n读取 6 路视频与事件文件"),
        ("同步回放", "USB 与 D435i 分页浏览\n按 recording_started / D435i 首帧时间对齐"),
        ("人工精修", "时间轴显示 experiment / pre-fog / fog\n表格编辑 FOG 起止"),
        ("标签导出", "imu_labeled.csv\ntime_labeled.csv\nfog_intervals_edited.csv"),
    ]
    body = [text(40, 48, "标注端工作流：从粗标签到模型训练标签", 28, COLORS["ink"], "900")]
    for index, (title, subtitle) in enumerate(columns):
        x = 70 + index * 300
        color = [COLORS["blue"], COLORS["cyan"], COLORS["orange"], COLORS["green"]][index]
        body.append(box(x, 130, 240, 125, title, subtitle, color, color))
        if index < len(columns) - 1:
            body.append(arrow(x + 240, 192, x + 290, 192))
    body.extend(
        [
            text(90, 345, "标签时间轴语义", 22, COLORS["ink"], "900"),
            '<rect x="90" y="380" width="1080" height="28" rx="14" fill="#D7DBE2"/>',
            '<rect x="310" y="380" width="160" height="28" rx="14" fill="#FCD34D" opacity=".85"/>',
            '<rect x="470" y="380" width="190" height="28" rx="14" fill="#EF4444" opacity=".8"/>',
            '<rect x="210" y="370" width="720" height="48" rx="20" fill="#3B82F6" opacity=".14" stroke="#2563EB" stroke-width="2"/>',
            text(250, 455, "experiment 区间", 16, COLORS["blue"], "800"),
            text(340, 360, "pre-fog", 16, COLORS["orange"], "800"),
            text(520, 360, "fog", 16, COLORS["red"], "800"),
            text(90, 505, "导出结果面向后续 FOG 检测建模：每条 IMU 样本增加 label 列，支持三分类或二分类。", 17, COLORS["ink"], "600"),
        ]
    )
    write_svg("03_annotation_pipeline.svg", 1280, 560, body)

    body = [
        text(40, 48, "工程工作量量化", 28, COLORS["ink"], "900"),
        text(40, 78, "统计对象：main.py / page1.py / page2.py 当前版本", 16, COLORS["muted"]),
    ]
    for index, (label, value, color) in enumerate(
        [
            ("核心代码行", totals["lines"], COLORS["blue"]),
            ("有效代码行", totals["loc"], COLORS["green"]),
            ("类", totals["classes"], COLORS["orange"]),
            ("函数/方法", totals["functions"], COLORS["purple"]),
        ]
    ):
        x = 70 + index * 290
        body.extend([box(x, 115, 230, 90, label, "", color, color), text(x + 115, 178, value, 34, color, "900", "middle")])
    body.append(text(70, 285, "各模块代码规模", 22, COLORS["ink"], "900"))
    max_lines = max(metrics[name]["lines"] for name in CORE_FILES)
    for index, name in enumerate(CORE_FILES):
        y = 325 + index * 64
        width = int(780 * metrics[name]["lines"] / max_lines)
        color = [COLORS["purple"], COLORS["blue"], COLORS["green"]][index]
        body.extend(
            [
                text(70, y + 25, name, 17, COLORS["ink"], "800"),
                f'<rect x="250" y="{y}" width="780" height="30" rx="5" fill="#E2E8F0"/>',
                f'<rect x="250" y="{y}" width="{width}" height="30" rx="5" fill="{color}"/>',
                text(250 + width + 12, y + 22, f'{metrics[name]["lines"]} 行 / {metrics[name]["functions"]} 函数', 15, COLORS["muted"], "700"),
            ]
        )
    body.append(text(70, 545, "实现覆盖面", 22, COLORS["ink"], "900"))
    features = [
        ("多设备接入", "4 USB + D435i + 5 IMU + 蓝牙"),
        ("同步记录", "session_start + events + sync"),
        ("实时反馈", "视频预览、IMU 表格、曲线、状态灯"),
        ("标注导出", "6 路回放、时间轴、标签 CSV"),
        ("工程交付", "README、PyInstaller、dist exe"),
    ]
    for index, (title, subtitle) in enumerate(features):
        body.append(box(70 + (index % 3) * 390, 585 + (index // 3) * 74, 340, 52, title, subtitle))
    write_svg("04_workload_metrics.svg", 1280, 760, body)

    artifacts = [
        ("session_metadata.json", "实验配置、设备清单、启用/跳过通道"),
        ("session_events.csv", "采集流程事件"),
        ("session_sync.csv", "各设备开始/停止时间"),
        ("imu.csv", "5 路 IMU 原始时序数据"),
        ("camera1-4.mp4", "USB 视频，按启用通道生成"),
        ("D435i/", "RGB.mp4 / Stereo.mp4 / frames.csv / metadata.json / depth raw"),
        ("remote_fog_events.csv", "遥控器实验与 FOG 事件"),
        ("remote_fog_intervals.csv", "遥控器粗 FOG 区间"),
        ("baselines/", "实验开始自动保存佩戴基线"),
        ("imu_labeled.csv", "标注后 IMU 数据"),
        ("time_labeled.csv", "关键时间点标签"),
        ("fog_intervals_edited.csv", "人工修正 FOG 区间"),
    ]
    body = [
        text(40, 48, "数据产物与可追溯性", 28, COLORS["ink"], "900"),
        text(40, 78, "一次实验输出一个 session 文件夹，原始数据、同步事件与人工标签分层保存。", 16, COLORS["muted"]),
    ]
    for index, (title, subtitle) in enumerate(artifacts):
        color = [COLORS["blue"], COLORS["green"], COLORS["orange"]][index % 3]
        body.append(box(70 + (index % 3) * 390, 125 + (index // 3) * 95, 340, 68, title, subtitle, color, color))
    write_svg("05_session_artifacts.svg", 1280, 560, body)

    body = [
        text(40, 48, "界面组织与实验操作路径", 28, COLORS["ink"], "900"),
        box(70, 115, 520, 360, "Page1 采集页", "面向实验现场：连设备、看状态、开始/停止采集、遥控打标", COLORS["blue"], COLORS["blue"]),
        box(110, 190, 200, 90, "IMU 实时页", "5 路状态表\n6 个实时曲线", COLORS["green"], COLORS["green"]),
        box(345, 190, 200, 90, "视频预览页", "USB / D435i 分页\n各自刷新按钮", COLORS["cyan"], COLORS["cyan"]),
        box(110, 320, 200, 90, "采集控制", "预检、保存路径\nD435i 开关", COLORS["orange"], COLORS["orange"]),
        box(345, 320, 200, 90, "遥控与校准", "双击实验标签\n角度置零/基线", COLORS["purple"], COLORS["purple"]),
        box(690, 115, 520, 360, "Page2 标注页", "面向离线整理：同步回放、修正 FOG 区间、导出训练标签", COLORS["red"], COLORS["red"]),
        box(730, 190, 200, 90, "视频同步回放", "USB(4路) / D435i(2路)\n分页浏览", COLORS["blue"], COLORS["blue"]),
        box(965, 190, 200, 90, "时间轴", "experiment\npre-fog / fog", COLORS["orange"], COLORS["orange"]),
        box(730, 320, 200, 90, "标签编辑", "表格编辑 FOG\n二/三分类模式", COLORS["purple"], COLORS["purple"]),
        box(965, 320, 200, 90, "导出", "imu_labeled\ntime_labeled", COLORS["green"], COLORS["green"]),
        arrow(590, 295, 690, 295),
    ]
    write_svg("06_ui_layout.svg", 1280, 560, body)


def write_reports(metrics: dict[str, dict[str, int]], totals: dict[str, int]) -> None:
    report = f"""# UpStudio FOG Tool 项目工作量汇报材料

生成时间：{datetime.now():%Y-%m-%d %H:%M}

## 1. 一句话结论

本项目已完成一个面向下肢外骨骼/FOG 实验的多源同步上位机：支持 **4 路 USB 相机、Intel RealSense D435i、5 个 WT IMU、蓝牙遥控器标签** 的现场同步采集，并提供离线 **6 路视频同步回放、FOG 标签精修、IMU 标签导出、exe 打包交付**。

![系统架构](images/01_system_architecture.svg)

## 2. 工程工作量量化

| 指标 | 数值 |
|---|---:|
| 核心 Python 文件 | {len(CORE_FILES)} 个 |
| 核心代码总行数 | {totals['lines']} 行 |
| 有效代码行 | {totals['loc']} 行 |
| 类 | {totals['classes']} 个 |
| 函数/方法 | {totals['functions']} 个 |

| 文件 | 总行数 | 有效代码行 | 类 | 函数/方法 | 主要职责 |
|---|---:|---:|---:|---:|---|
| main.py | {metrics['main.py']['lines']} | {metrics['main.py']['loc']} | {metrics['main.py']['classes']} | {metrics['main.py']['functions']} | 主窗口、页面切换、遥控器按键全局捕获 |
| page1.py | {metrics['page1.py']['lines']} | {metrics['page1.py']['loc']} | {metrics['page1.py']['classes']} | {metrics['page1.py']['functions']} | 采集端、IMU UDP、USB/D435i 视频、蓝牙标签、基线、同步账本 |
| page2.py | {metrics['page2.py']['lines']} | {metrics['page2.py']['loc']} | {metrics['page2.py']['classes']} | {metrics['page2.py']['functions']} | 标注端、6 路视频同步回放、时间轴、标签编辑与导出 |

![工作量统计](images/04_workload_metrics.svg)

## 3. 已实现的核心功能

### 3.1 采集端 Page1

- 多设备接入：4 路 USB Camera、D435i RGB/Stereo/depth、5 个 WT IMU、蓝牙遥控器。
- 设备预检：开始采集前检查 IMU、相机、D435i、蓝牙遥控器；USB Camera 可单通道选择“不使用”并跳过该路预检。
- 实时反馈：IMU 状态表、实时曲线、USB/D435i 分页视频预览、遥控器连接与按键状态。
- 同步采集：统一 session_start 时间零点，写入 session_events.csv 和 session_sync.csv。
- 遥控标签：单击 FOG 起止，1 秒内双击实验起止；实验开始自动保存佩戴基线。
- IMU 操作：支持在线 IMU 加速度计校准、角度置零、佩戴基线记录。
- 工程交付：PyInstaller spec 与 build_exe.ps1，可生成 Windows 可执行程序。

![采集流程](images/02_acquisition_pipeline.svg)

### 3.2 标注端 Page2

- 自动加载 session：切换到标注页时自动读取当前或最近 session。
- 6 路同步回放：camera1-4 与 D435i RGB/Stereo 同步播放，USB 与 D435i 分页显示。
- 时间对齐：USB 使用 recording_started 事件，D435i 使用 frames.csv 首帧时间，对齐到 session 时间轴。
- 标签精修：时间轴显示 experiment、pre-fog、fog；支持表格编辑 FOG 起止时间。
- 标签模式：支持三分类 normal/pre-fog/fog 和二分类 normal/fog。
- 导出结果：生成 imu_labeled.csv、time_labeled.csv、fog_intervals_edited.csv。

![标注流程](images/03_annotation_pipeline.svg)

## 4. 数据产物

一次采集会生成完整 session 文件夹，包含原始传感器数据、视频、同步账本、遥控粗标签、基线文件和后处理标签。

![数据产物](images/05_session_artifacts.svg)

## 5. 界面与操作路径

项目按实验现场和离线标注两个场景拆分：Page1 负责采集，Page2 负责标注导出。视频部分已经按 USB 与 D435i 分页，避免 6 路画面挤在同一界面。

![界面组织](images/06_ui_layout.svg)

## 6. 适合组会强调的结论

1. 从脚本采集升级为完整实验上位机：已覆盖设备接入、预检、采集、同步、标注、导出、打包。
2. 数据链路可追溯：每个 session 都有 metadata、events、sync 三类账本，后续可以复查每路设备的起止时间。
3. FOG 标签闭环已打通：蓝牙遥控器产生粗标签，Page2 基于视频精修，最终落到 IMU 样本级 label。
4. 现场容错能力增强：D435i 可选，USB Camera 可单通道跳过，设备异常会在预检阶段拦截。
5. 已具备交付形态：项目可打包为 dist/UpStudioFOGTool/UpStudioFOGTool.exe，方便迁移到实验电脑。

## 7. 下一阶段建议

- 做多被试/长时长采集压力测试，重点统计 IMU UDP 丢包和 D435i 磁盘写入稳定性。
- 增加采集后质量报告：每路相机文件是否生成、IMU 包计数缺口、标签区间完整性。
- 明确实验 SOP：设备摆放、Wi-Fi 信道、IMU 目标 IP/端口、遥控器单击/双击规则。
- 根据实验需要决定是否在实验开始时自动执行角度置零；目前只自动保存佩戴基线，不自动角度置零。
"""
    (OUT / "project_workload_report.md").write_text(report, encoding="utf-8")

    outline = f"""# 组会 PPT 提纲：UpStudio FOG Tool 工作量汇报

## 1. 背景与目标
- 下肢外骨骼/FOG 实验需要同步采集多源数据。
- 目标：把 USB 视频、D435i、WT IMU、遥控器标签统一到一个 session 中。
- 配图：images/01_system_architecture.svg

## 2. 当前完成度总览
- 4 路 USB Camera + D435i RGB/Stereo/depth + 5 个 WT IMU + 蓝牙遥控器。
- Page1 采集，Page2 标注，PyInstaller 打包。
- 核心代码 {totals['lines']} 行，有效代码 {totals['loc']} 行，{totals['functions']} 个函数/方法。
- 配图：images/04_workload_metrics.svg

## 3. 采集端工作量
- 设备预检与状态显示。
- USB/D435i 分页预览与刷新。
- IMU UDP 接收与写盘队列。
- session_events/session_sync 同步账本。
- 蓝牙单击/双击标签与自动佩戴基线。
- 配图：images/02_acquisition_pipeline.svg

## 4. 标注端工作量
- 6 路视频同步回放。
- 时间轴可视化 experiment/pre-fog/fog。
- FOG 区间精修表格。
- 生成 imu_labeled.csv、time_labeled.csv、fog_intervals_edited.csv。
- 配图：images/03_annotation_pipeline.svg

## 5. 数据产物与可追溯性
- 原始数据、视频、标签、同步信息、基线文件都进入 session 文件夹。
- 支持后续复查设备起止、标签来源和导出结果。
- 配图：images/05_session_artifacts.svg

## 6. 界面与实验流程
- Page1 用于现场采集。
- Page2 用于离线标注。
- 视频浏览已拆分 USB/D435i，改善 6 路视频拥挤问题。
- 配图：images/06_ui_layout.svg

## 7. 下一步计划
- 长时长稳定性测试。
- IMU 丢包率自动统计。
- 采集质量报告自动生成。
- 实验 SOP 固化。
"""
    (OUT / "ppt_outline.md").write_text(outline, encoding="utf-8")

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>UpStudio FOG Tool 工作量汇报</title>
<style>
body {{ margin: 0; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; color: #17202A; background: #E5E7EB; }}
.slide {{ width: 1180px; min-height: 720px; margin: 28px auto; padding: 44px 56px; background: #fff; box-shadow: 0 10px 28px rgba(15,23,42,.18); box-sizing: border-box; page-break-after: always; }}
h1 {{ margin: 0 0 14px; font-size: 40px; }}
h2 {{ margin: 0 0 22px; font-size: 30px; }}
p, li {{ font-size: 22px; line-height: 1.55; }}
.small {{ color: #5D6D7E; font-size: 18px; }}
.grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; margin: 30px 0; }}
.card {{ border: 1px solid #CBD5E1; border-radius: 8px; padding: 20px; background: #F8FAFC; }}
.num {{ font-size: 38px; color: #2F6BFF; font-weight: 800; }}
img {{ max-width: 100%; display: block; margin: 20px auto 0; }}
@media print {{ body {{ background: #fff; }} .slide {{ margin: 0; box-shadow: none; width: 100%; min-height: 0; }} }}
</style>
</head>
<body>
<section class="slide"><h1>UpStudio FOG Tool 工作量汇报</h1><p>面向下肢外骨骼/FOG 实验的多源同步采集与标注上位机。</p><div class="grid"><div class="card"><div class="num">4</div><p>USB Camera</p></div><div class="card"><div class="num">D435i</div><p>RGB / Stereo / depth</p></div><div class="card"><div class="num">5</div><p>WT IMU</p></div><div class="card"><div class="num">6</div><p>视频同步标注路数</p></div></div><p class="small">生成时间：{datetime.now():%Y-%m-%d %H:%M}</p></section>
<section class="slide"><h2>1. 系统架构</h2><p>采集端、数据账本和标注端已经形成闭环。</p><img src="images/01_system_architecture.svg"></section>
<section class="slide"><h2>2. 工程工作量量化</h2><p>核心 Python 代码 {totals['lines']} 行，有效代码 {totals['loc']} 行，{totals['classes']} 个类，{totals['functions']} 个函数/方法。</p><img src="images/04_workload_metrics.svg"></section>
<section class="slide"><h2>3. 采集端工作流</h2><p>统一时间零点，多线程/队列写盘，遥控器标签进入同一 session。</p><img src="images/02_acquisition_pipeline.svg"></section>
<section class="slide"><h2>4. 标注端工作流</h2><p>6 路视频同步回放，FOG 标签精修后导出到 IMU 样本级标签。</p><img src="images/03_annotation_pipeline.svg"></section>
<section class="slide"><h2>5. 数据产物</h2><p>每次实验形成完整 session 文件夹，方便追溯、复查和建模。</p><img src="images/05_session_artifacts.svg"></section>
<section class="slide"><h2>6. 界面组织</h2><p>Page1 面向采集现场，Page2 面向离线标注；USB 与 D435i 视频已分页。</p><img src="images/06_ui_layout.svg"></section>
<section class="slide"><h2>7. 汇报结论</h2><ul><li>多源同步采集、标签和导出流程已打通。</li><li>session metadata/events/sync 保证数据链路可追溯。</li><li>支持 exe 打包，具备在实验电脑部署的交付形态。</li><li>下一步建议重点做长时长稳定性和 IMU 丢包统计。</li></ul></section>
</body>
</html>
"""
    (OUT / "UpStudioFOGTool_workload_report.html").write_text(html_doc, encoding="utf-8")

    rows = ["file,lines,loc,classes,functions"]
    rows.extend(
        f"{name},{metrics[name]['lines']},{metrics[name]['loc']},{metrics[name]['classes']},{metrics[name]['functions']}"
        for name in CORE_FILES
    )
    rows.append(f"TOTAL,{totals['lines']},{totals['loc']},{totals['classes']},{totals['functions']}")
    (OUT / "code_metrics.csv").write_text("\n".join(rows), encoding="utf-8")


def main() -> None:
    metrics, totals = collect_metrics()
    generate_svgs(metrics, totals)
    write_reports(metrics, totals)
    print(f"Generated presentation assets in {OUT}")


if __name__ == "__main__":
    main()
