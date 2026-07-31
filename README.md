# UpStudio FOG Data Tool

UpStudio FOG Data Tool 是一个基于 PyQt6 的下肢外骨骼/FOG 实验上位机工具，用于同步采集 USB 相机、Intel RealSense D435i、WT 系列 IMU、8 路低频 EMG、3 路 EEG 以及蓝牙遥控器标签数据，并提供视频回放与 FOG 标签精修导出功能。

## 主要功能

- Page1 采集界面
  - 采集 4 路 USB 相机视频。
  - 可选采集 Intel RealSense D435i RGB、Stereo、depth raw 及相关帧时间信息。
  - 通过 UDP 接收 5 个 WT IMU 数据。
  - 通过 UDP 接收并显示 8 路低频 EMG（`000001`、`000002`）和 3 路 EEG（`000003`）。
  - 检查相机、IMU、D435i、蓝牙遥控器连接状态。
  - 记录佩戴基线。
  - 对在线 IMU 执行“角度置零”。
  - 使用蓝牙遥控器进行实验开始/结束和 FOG 开始/结束打标签。

- Page2 标注界面
  - 预加载当前或最近一次实验视频。
  - 加载蓝牙遥控器生成的粗标签。
  - 根据视频手动修改 FOG 开始/结束时间。
  - 在时间轴和表格中显示实验开始/结束时间。
  - 流式导出带标签的 IMU、EMG、EEG 数据和关键时间标签文件。

## 设备要求

- Windows 10/11。
- Python 环境，当前开发调试环境为 Anaconda `video` 环境，Python 3.10.20。
- 4 个 USB 相机。
- 5 个 WT IMU，使用 UDP 发送数据到上位机。
- 2 个四通道低频 EMG 设备（序列号 `000001`、`000002`）和 1 个三通道 EEG 设备（序列号 `000003`）。
- Intel RealSense D435i，可在界面中选择是否启用。
- 蓝牙 LE 单按钮遥控器，连接到 Windows 后按键映射为音量键。

## 安装依赖

进入项目目录后，建议使用已有的 `video` 环境：

```powershell
conda activate video
python -m pip install .
```

## 启动程序

```powershell
conda activate video
python main.py
```

程序入口为 `main.py`。启动后包含两个页面：

- `Page1 采集`：设备检查、IMU/EMG/EEG/视频采集、双生理信号子框、蓝牙遥控器标签和 IMU 操作。
- `Page2 标注`：视频回放、标签修正、标签文件生成。

## 采集前准备

1. 确认 5 个 WT IMU 与电脑位于同一 Wi-Fi 网络。
2. 将 IMU 的 UDP 目标地址设置为电脑当前 IPv4 地址，目标端口为 `1399`。
3. 确认三台 EMG/EEG 设备与电脑位于同一网络。程序默认监听数据端口 `30300`、设备信息端口 `30301`，并向 `30200` 自动广播接收地址。
4. 关闭独立的 `emg_program`，避免它与本程序争用 UDP `30300/30301`。
5. 确认 Windows 防火墙允许当前 Python 程序接收 UDP 数据。
6. 连接 4 个 USB 相机。
7. 如需 D435i 数据，在界面中勾选“开启 D435i 视频采集”，并确认 D435i 已连接。
8. 将蓝牙遥控器连接到 Windows，并在蓝牙遥控器框内刷新连接状态。

## 本机实时 IMU 控制流

采集程序在收到每个 WT IMU UDP 数据包后，会立即把完全相同的原始数据包
非阻塞转发到：

```text
127.0.0.1:15100/UDP
```

该流用于电刺激时机判断等独立实时控制算法，不需要等待 `imu.csv` 写盘：

```text
WT IMU -> 采集程序 UDP 1399 -> 本机控制流 UDP 15100 -> 独立控制算法
```

- IMU 仍然只向采集程序的 `1399` 端口发送数据，无需修改设备配置。
- `30300` 仍是 EMG/EEG 接收端口，与本机 IMU 控制流无关。
- 转发内容是采集程序收到的原始 WT UDP 数据包，设备编号、时间和九轴数据均保持不变。
- 转发采用非阻塞尽力发送；算法程序未启动、退出或处理过慢时，不会阻塞采集、显示或写盘。
- 算法程序应仅监听本机回环地址，不要将 `15100` 暴露到局域网。

最小订阅示例：

```python
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", 15100))

while True:
    raw_wt_datagram, _ = sock.recvfrom(8192)
    # 在独立控制程序中解析和处理 raw_wt_datagram
```

点击“开始采集”前，程序会对关键设备做预检：

- 5 个 IMU 需要在线。
- 2 个 EMG 和 1 个 EEG 设备需要在线。
- 11 个 EMG/EEG 通道都需要在最近 3 秒内收到有效数据帧。
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

每次采集会在数据目录下生成一个独立 session 文件夹。默认数据目录为 Windows“文档”下的 `UpStudioFOGTool/data`，与程序构建目录分离，也可在界面中修改。常见文件结构如下：

```text
session/
  session_metadata.json
  session_events.csv
  session_sync.csv
  imu.csv
  emg.csv
  eeg.csv
  imu_labeled.csv          标注导出后生成
  emg_labeled.csv          标注导出后生成
  eeg_labeled.csv          标注导出后生成
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
- `emg.csv`：8 路低频肌电原始值；`000001` 对应通道 1–4，`000002` 对应通道 5–8，并保留 `packet_serial_number` 便于按设备拆分。
- `eeg.csv`：`000003` 的 3 路脑电原始值。
- 接收器严格按帧内序列号路由；未知序列号或序列号与信号类型不一致的帧会被拒绝，不会按来源 IP 猜测设备。
- `emg.csv` 与 `eeg.csv` 按 PC 收包时间和 1000 Hz 理论采样间隔重建 `sync_timestamp`；正常接收时连续递增，遇到明显断连/重连时会重新锚定到重连包时间，使时间轴保留断连空洞。设备时间戳仅用于内部诊断，不写入 CSV。
- `emg.csv` 字段依次为 `world_time`、`sync_timestamp`、`packet_serial_number`、`channel`、`value_uV`。
- `eeg.csv` 字段依次为 `world_time`、`sync_timestamp`、`channel`、`value_uV`。
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

- `imu_labeled.csv`、`emg_labeled.csv`、`eeg_labeled.csv`
  - 分别在三个原始 CSV 基础上增加 `label` 列；导出采用逐行流式处理，不会一次性把长时间生理信号文件载入内存。
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

打包脚本会清理旧的 `dist/UpStudioFOGTool`。若检测到旧分发目录内存在 `data` 实验文件，脚本会主动取消构建；请先把数据移动到安全位置，切勿把实验数据长期保存在 `dist` 下。

## 测试

协议、序列号映射、起始边界裁剪和停止尾队列回归测试：

```powershell
python -m unittest discover -s tests -v
```

## 长时间采集提示

EMG/EEG 采用便于追溯的长表 CSV，每秒约 11,000 行。按当前字段估算约为 45 MiB/分钟、2.7 GiB/小时，Excel 的单表行数上限也不适合直接打开长时间记录。正式实验建议选择空间充足的本地 SSD（避免实时写入 OneDrive/网盘同步目录），使用 Python、MATLAB、R 等工具按 `sync_timestamp` 和 `channel` 分组分析，并提前做足时长压力测试。

## 项目结构

```text
main.py                  程序入口和主窗口
page1.py                 采集界面、设备接收、数据保存、蓝牙遥控器标签
biosignal.py             EMG/EEG UDP 协议、接收线程、保存与双子框波形组件
page2.py                 视频回放、标签编辑、标签导出
pyproject.toml           Python 项目依赖配置
upstudio_fog_tool.spec   PyInstaller 打包配置
build_exe.ps1            Windows 打包脚本
tests/                   生理信号协议、边界与并发回归测试
```

## 常见问题

### IMU 无法连接

- 确认电脑和 IMU 在同一 Wi-Fi 网络。
- 确认 IMU 的 UDP 目标 IP 是电脑当前 IPv4 地址。
- 确认目标端口为 `1399`。
- 确认没有其他程序占用 UDP `1399` 端口。
- 确认 Windows 防火墙允许 Python 接收 UDP 数据。

### EMG/EEG 无法连接

- 确认三台设备序列号分别为 `000001`、`000002`、`000003`，且与电脑在同一局域网。
- 确认独立 `emg_program` 已关闭；同一时刻只能有一个程序占用 UDP `30300/30301`。
- 在“EMG / EEG 接收”框中确认端口为 `30300`，然后点击“重新监听”。
- 确认 Windows 防火墙允许 UDP `30200`、`30300` 和 `30301`。

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
