# VPS 一键部署脚本

Cloudflare DDNS + nyanpass + komari-agent + Telegram IP 变更通知

## 仓库结构

```
├── setup.sh            # 主部署脚本（公开，无敏感信息）
├── config.env.example  # 配置模板（公开，仅占位符）
├── .gitignore          # 忽略 config.env
└── README.md
```

## 使用方法

### 1. 准备配置

```bash
cp config.env.example config.env
vim config.env   # 填入真实值
```

### 2. VPS 上执行（两种方式）

**方式 A：本地填好后上传执行**
```bash
source config.env
sudo -E bash setup.sh
```

**方式 B：直接在 VPS 导出变量后拉取执行**
```bash
export API_TOKEN="你的值"
export ZONE_ID="你的值"
export RECORD_NAME="你的域名"
export TG_BOT_TOKEN="你的值"
export TG_CHAT_ID="你的值"

bash <(curl -fsSL https://raw.githubusercontent.com/你的用户名/仓库名/main/setup.sh)
```

## 注意事项

- `config.env` 已加入 `.gitignore`，不会被提交到仓库
- 敏感信息仅存在于本地终端会话和 VPS 本机的 `/usr/local/bin/cf-ddns-dual.sh`
- VPS 上生成的 DDNS 脚本含有真实密钥，注意控制 VPS 访问权限
