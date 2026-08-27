# OpenWorldSandbox 数据说明

本仓库**只包含代码与数据格式规格文档**（v0.1 规格见 `scenarios/README.md` 与 `tasks/README.md`）。

全部场景与任务数据托管在 HuggingFace 数据集：

**https://huggingface.co/datasets/zetliu2001/OpenWorldSandbox**

## 获取数据

```bash
# 方式一：hf CLI（下载到 data/ 下对应目录）
hf download zetliu2001/OpenWorldSandbox \
  --repo-type dataset \
  --include "scenarios/*" "tasks/**" \
  --local-dir data

# 方式二：git 克隆数据集仓库后手动放置
git clone https://huggingface.co/datasets/zetliu2001/OpenWorldSandbox /tmp/ows-data
cp -r /tmp/ows-data/scenarios /tmp/ows-data/tasks data/
```

## 目录结构（本地获取后）

```
data/
├── scenarios/            # 场景包（JSON v0.1）
│   ├── README.md         # 场景规格文档（随代码仓库维护）
│   └── <scenario_id>.json
└── tasks/                # 任务包（JSON v0.1）
    ├── README.md         # 任务规格文档（随代码仓库维护）
    ├── home/             # home_01 场景的任务
    └── market/           # market_01 场景的任务
```

## 数据更新流程

1. 本地在 `data/` 下生成/修改数据（JSON 不入 git）。
2. 上传到 HF 数据集：

   ```bash
   hf upload zetliu2001/OpenWorldSandbox data/scenarios scenarios --repo-type dataset
   hf upload zetliu2001/OpenWorldSandbox data/tasks tasks --repo-type dataset
   ```
