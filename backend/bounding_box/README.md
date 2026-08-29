# Trajectory Action Box Project

这个独立工程为 `20260711_104625` 下每个存在的
`stepXXX_vla_input_stability.jpg` 生成且只生成一个动作框，并通过 ttk 工具浏览结果。
默认流程为“规则候选框 + Qwen3.6-27B 视觉复核”。

## 框的定义

1. 规则先产生候选框：click 使用 clickable 节点，swipe 使用 scrollable 容器；type 直接跳过，不生成框。
2. 从 `stepXXX_vla_model_response.json` 读取 `<summary>` 作为 action_summary。
3. Qwen3.6-27B 查看原始 stability 截图上的候选框，并结合 action 与 action_summary 判断框是否合适。
4. 合适则确认；不合适则返回新的唯一 bbox，再把新框交给模型复核。
5. 默认最多复核 4 轮，每轮结果、原因和置信度都写入 manifest，并由 ttk 工具展示。

模型复核使用横纵轴分别归一化到 0～1000 的坐标。复核图会额外显示实际 action
点作为约束：click 框必须包含实际点击点，swipe 框必须包含起点和终点；最终输出图仍然只画一个框。
如果模型连续返回几乎相同的框，则记录为收敛验证完成。

动作坐标按设备/UI XML 像素坐标处理，并在 UI 尺寸和截图尺寸不一致时自动缩放。

## 使用

双击：

```text
run_viewer.cmd
```

重新生成所有标框图：

```text
run_build.cmd
```

运行前需要配置模型 API：

```dotenv
# 在仓库根目录的 backend/.env 中配置；不再把地址写进脚本或命令行。
MODEL_API_KEY=你的密钥
MODEL_BASE_URL=http://你的内部模型网关/v1
BBOX_MODEL=qwen3.6-27b:floor
```

```powershell
python build_annotations.py
```

也兼容旧的 `TRAJECTORY_*` 环境变量。模型响应会缓存到 `qwen_review_cache.json`，
重复运行不会再次请求已经完成的相同复核。只有明确传入 `--rules-only` 时才会跳过模型。

也可以从终端运行：

```powershell
python build_annotations.py
python viewer.py
```

默认输入目录：

```text
C:\Users\panda\Desktop\gui-trajectory-adarubric-project\trajectories\20260711_104625
```

输出在 `annotated/<trajectory>/stepXXX_boxed.jpg`，总清单为
`annotated/manifest.json`。

ttk viewer 默认额外显示原始动作：click 使用黄色十字圆点，swipe 使用带 S/E
端点的黄色方向箭头；左侧步骤树也会列出全部原始坐标。可通过工具栏的
`Show raw click/swipe` 随时关闭。该叠加层只存在于 viewer，不会写入标框图片。

## 当前数据完整性

原始数据共有 47 个动作。`AT-YYSP-AQY-009/step006` 是 type 动作，按当前规则
直接跳过，不生成框，也不计为缺失。其余 46 个 click/swipe 动作均生成标框图。
