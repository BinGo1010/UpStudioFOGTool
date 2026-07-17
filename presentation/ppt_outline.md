# 组会 PPT 提纲：UpStudio FOG Tool 工作量汇报

## 1. 背景与目标
- 下肢外骨骼/FOG 实验需要同步采集多源数据。
- 目标：把 USB 视频、D435i、WT IMU、遥控器标签统一到一个 session 中。
- 配图：images/01_system_architecture.svg

## 2. 当前完成度总览
- 4 路 USB Camera + D435i RGB/Stereo/depth + 5 个 WT IMU + 蓝牙遥控器。
- Page1 采集，Page2 标注，PyInstaller 打包。
- 核心代码 3988 行，有效代码 3622 行，187 个函数/方法。
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
