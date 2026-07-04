# ContextAuthServer 部署（deploy/）

摄取服务（FastAPI，`app/`）的自包含容器化部署。**数据与配置均在本目录下**，不写入仓库其它位置。

## 目录布局
```
deploy/
├── docker-compose.yml     # 部署编排（含构建代理、挂载、端口、健康检查）
├── README.md              # 本文件
├── data/paper/            # 挂载到容器 /data/paper（数据根）
│   ├── rules.json         # 脱敏规则（已由 app/default_rules.json seed）
│   ├── server_study_salt.txt  # 首次启动生成（0600）
│   ├── devices/           # 按 device_id/date 落盘的批次
│   ├── index/             # devices/batches/errors 索引 jsonl
│   └── quarantine/        # 校验失败隔离
└── logs/                  # 挂载到容器 /app/logs
```

## 构建与启动（在仓库根 `ContextAuthServer/` 下执行）
```bash
# 1) 构建镜像（构建时容器内部代理 = http://192.168.128.2:9999，--network host）
docker compose -f deploy/docker-compose.yml build

# 2) 启动（后台）
docker compose -f deploy/docker-compose.yml up -d

# 3) 状态 / 健康 / 日志
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f

# 4) 停止并移除
docker compose -f deploy/docker-compose.yml down
```

## 验证
```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/api/v1/config
curl -fsS http://127.0.0.1:8000/api/v1/rules
curl -fsS http://127.0.0.1:8000/metrics | head
```

## 关键说明
- **端点仅 6 个**：`/health` `/ready` `/api/v1/config` `/api/v1/rules` `/api/v1/ingest` `/metrics`；无鉴权、无 dashboard、不解密（详见 `docs/ContextAuthServer_服务端说明.md`）。
- **端口（2026-07-05 订正，DOC-5）**：`deploy/docker-compose.yml` 的 `ports` 默认即 **`0.0.0.0:8000:8000`**——**对外监听所有网卡，并非仅本机**。服务**无鉴权、应用层不加密**，因此对外暴露时**必须**用防火墙 / 白名单限制来源，或前置反向代理启用 HTTPS/TLS。**若只需本机访问，请把 `ports` 改为 `127.0.0.1:8000:8000`。**
- **属主**：容器以非 root（`APP_UID/GID=1001`=宿主用户）运行，`deploy/data`、`deploy/logs` 文件属主即宿主用户，可直接读写。
- **构建代理**：仅构建期使用 `192.168.128.2:9999`；镜像不持久化该代理。基础镜像 `python:3.11-slim-bookworm` 已在本机，pip 依赖来自 `vendor/wheels` 离线安装。
- **研究实验层 `research/`** 不在此服务容器内运行（它是离线 ML 实验工具，用 conda env `hmog_1dcnn` 单独运行，见 `research/README.md`）。
