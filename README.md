# UpStudio FOG Data Tool

UpStudio FOG Data Tool 是一个基于 PyQt6 的下肢外骨骼/FOG 实验上位机工具，用于同步采集 USB 相机、Intel RealSense D435i、WT 系列 IMU 以及蓝牙遥控器标签数据，并提供视频回放与 FOG 标签精修导出功能。

## 主要功能

- Page1 采集界面
  - 采集 4 路 USB 相机视频。
  - 可选采集 Intel RealSense D435i RGB、Stereo、depth raw 及相关帧时间信息。
  - 通过 UDP 接收 5 个 WT IMU 数据。
  - 检查相机、IMU、D435i、蓝牙遥控器连接状态。
  - 记录佩戴基线。
  - 对在线 IMU 执行“角度置零”。
  - 使用蓝牙遥控器进行实验开始/结束和 FOG 开始/结束打标签。

- Page2 标注界面
  - 预加载当前或最近一次实验视频。
  - 加载蓝牙遥控器生成的粗标签。
  - 根据视频手动修改 FOG 开始/结束时间。
  - 在时间轴和表格中显示实验开始/结束时间。
  - 导出带标签的 IMU 数据和关键时间标签文件。

## 设备要求

- Windows 10/11。
- Python 环境，当前开发调试环境为 Anaconda `video` 环境，Python 3.10.20。
- 4 个 USB 相机。
- 5 个 WT IMU，使用 UDP 发送数据到上位机。
- Intel RealSense D435i，可在界面中选择是否启用。
- 蓝牙 LE 单按钮遥控器，连接到 Windows 后按键映射为音量键。

## 安装依赖

进入项目目录后，建议使用已有的 `video` 环境：

```powershell
conda activate video
python -m pip install PyQt6==6.9.1 pyqtgraph numpy opencv-python pyrealsense2 pyinstaller
```

如果使用 `pyproject.toml` 管理依赖，请确认其中的 Python 版本声明与当前环境一致。

## 启动程序

```powershell
conda activate video
python main.py
```

程序入口为 `main.py`。启动后包含两个页面：

- `Page1 采集`：设备检查、数据采集、蓝牙遥控器标签、IMU 操作。
- `Page2 标注`：视频回放、标签修正、标签文件生成。

## 采集前准备

1. 确认 5 个 WT IMU 与电脑位于同一 Wi-Fi 网络。
2. 将 IMU 的 UDP 目标地址设置为电脑当前 IPv4 地址，目标端口为 `1399`。
3. 确认 Windows 防火墙允许当前 Python 程序接收 UDP 数据。
4. 连接 4 个 USB 相机。
5. 如需 D435i 数据，在界面中勾选“开启 D435i 视频采集”，并确认 D435i 已连接。
6. 将蓝牙遥控器连接到 Windows，并在蓝牙遥控器框内刷新连接状态。

点击“开始采集”前，程序会对关键设备做预检：

- 5 个 IMU 需要在线。
- 4 个 USB 相机需要在线。
- 若开启 D435i 视频采集，D435i 需要在线。
- 蓝牙遥控器需要处于连接状态。

## 蓝牙遥控器标签规则

蓝牙遥控器支持单击和双击两种动作：

- 单击：切换 FOG 标签。
  - 第一次单击表示 FOG 开始。
  - 再次单击表示 FOG 结束。
  - 为避免与双击冲突，单击会等待 1 秒双击窗口后再确认。

- 1 秒内双击：切换实验标签。
  - 第一次双击表示实验开始。
  - 再次双击表示实验结束。
  - 实验开始时会自动记录一次佩戴基线。

蓝牙遥控器当前接受的按键码：

- `Qt.Key_VolumeUp`
- `Qt.Key_VolumeDown`

## 数据输出

每次采集会在数据目录下生成一个独立 session 文件夹。常见文件结构如下：

```text
session/
  session_metadata.json
  session_events.csv
  session_sync.csv
  imu.csv
  camera1.mp4
  camera2.mp4
  camera3.mp4
  camera4.mp4
  remote_fog_events.csv
  remote_fog_intervals.csv
  baselines/
    *_wearing_baseline.csv
  D435i/
    RGB.mp4
    Stereo.mp4
    frames.csv
    metadata.json
```

说明：

- `imu.csv`：WT IMU 原始采集数据，时间戳相对本次 session 开始时间。
- `camera1.mp4` 到 `camera4.mp4`：四路 USB 相机视频。
- `D435i/`：仅在开启 D435i 视频采集时生成。
- `session_events.csv`：采集流程事件。
- `session_sync.csv`：各设备开始/停止时间记录，用于后续对齐。
- `remote_fog_events.csv`：蓝牙遥控器所有事件，包括 FOG 与实验开始/结束。
- `remote_fog_intervals.csv`：仅保存 FOG 区间。
- `baselines/`：佩戴基线文件。

## 标注与导出

进入 `Page2 标注` 后，程序会自动加载当前或最近一次 session，也可以手动选择 session 文件夹。

标注流程：

1. 加载原始视频和遥控器粗标签。
2. 根据视频画面，在右侧表格中编辑 FOG 开始/结束时间。
3. 根据需要设置 Pre-FOG 时长和标签模式。
4. 点击“生成标签文件”。

导出文件：

- `imu_labeled.csv`
  - 在 `imu.csv` 基础上增加 `label` 列。
  - 标签含义：
    - `0`：normal
    - `1`：pre-fog
    - `2`：fog

- `time_labeled.csv`
  - 保存关键时间点。
  - 包含：
    - `experiment_start`
    - `experiment_end`
    - `pre_fog_start`
    - `fog_start`
    - `fog_end`

- `fog_intervals_edited.csv`
  - 保存人工修正后的 FOG 区间。

## 打包为 exe

项目提供了 PyInstaller 打包脚本：

```powershell
conda activate video
.\build_exe.ps1
```

打包完成后输出：

```text
dist/UpStudioFOGTool/UpStudioFOGTool.exe
```

迁移到其他 Windows 电脑时，请复制整个 `dist/UpStudioFOGTool` 文件夹，而不是只复制单个 exe。

## 项目结构

```text
main.py                  程序入口和主窗口
page1.py                 采集界面、设备接收、数据保存、蓝牙遥控器标签
page2.py                 视频回放、标签编辑、标签导出
pyproject.toml           Python 项目依赖配置
upstudio_fog_tool.spec   PyInstaller 打包配置
build_exe.ps1            Windows 打包脚本
data/                    默认实验数据目录
```

## 常见问题

### IMU 无法连接

- 确认电脑和 IMU 在同一 Wi-Fi 网络。
- 确认 IMU 的 UDP 目标 IP 是电脑当前 IPv4 地址。
- 确认目标端口为 `1399`。
- 确认没有其他程序占用 UDP `1399` 端口。
- 确认 Windows 防火墙允许 Python 接收 UDP 数据。

### D435i 无法采集

- 确认 D435i 已插入并能被 Intel RealSense 工具识别。
- 确认安装了 `pyrealsense2`。
- 如果本次实验不需要 D435i，可以取消勾选“开启 D435i 视频采集”。

### 蓝牙遥控器显示未连接

- 在 Windows 蓝牙设置中重新连接遥控器。
- 点击界面中的蓝牙遥控器状态刷新按钮。
- 确认按下遥控器时 Windows 能收到音量键事件。

### GitHub 上传失败

如果 HTTPS 推送失败，可以改用 SSH：

```powershell
git remote set-url origin git@github.com:BinGo1010/UpStudioFOGTool.git
git push -u origin main
```

若 Windows 用户名包含中文导致 SSH known hosts 写入失败，可以使用独立的英文路径保存 SSH key 和 known_hosts。
