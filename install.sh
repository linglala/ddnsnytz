#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}"
echo "╔══════════════════════════════════════╗"
echo "║       AWS.sb 面板 一键安装           ║"
echo "╚══════════════════════════════════════╝"
echo -e "${NC}"

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}请使用 root 权限运行: sudo bash install.sh${NC}"
  exit 1
fi

INSTALL_DIR="/opt/awssb-panel"
SERVICE_NAME="awssb-panel"
REPO="linglala/awsxzs"

echo -e "${YELLOW}请设置面板配置：${NC}"
read -p "面板端口 [默认 5000]: " PORT
PORT=${PORT:-5000}
read -p "管理员用户名 [默认 admin]: " PANEL_USER
PANEL_USER=${PANEL_USER:-admin}
read -s -p "管理员密码 [默认 admin123]: " PANEL_PASS
echo
PANEL_PASS=${PANEL_PASS:-admin123}

echo -e "\n${GREEN}[1/6] 更新系统并安装依赖...${NC}"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl iputils-ping netcat-openbsd

echo -e "${GREEN}[2/6] 创建安装目录...${NC}"
mkdir -p $INSTALL_DIR
cd $INSTALL_DIR

echo -e "${GREEN}[3/6] 下载项目文件...${NC}"
if [ -d ".git" ]; then
  git pull
else
  MIRRORS=(
    "https://ghfast.top/https://github.com/${REPO}"
    "https://gh-proxy.com/https://github.com/${REPO}"
    "https://mirror.ghproxy.com/https://github.com/${REPO}"
    "https://github.com/${REPO}"
  )
  CLONED=false
  for MIRROR in "${MIRRORS[@]}"; do
    echo -e "尝试: $MIRROR"
    if git clone "$MIRROR" . 2>/dev/null; then
      echo -e "${GREEN}下载成功${NC}"
      CLONED=true
      break
    fi
  done
  if [ "$CLONED" = false ]; then
    echo -e "${RED}所有镜像下载失败，请手动上传文件到 ${INSTALL_DIR}${NC}"
    exit 1
  fi
fi

echo -e "${GREEN}[4/6] 安装 Python 依赖...${NC}"
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt

echo -e "${GREEN}[5/6] 创建配置文件...${NC}"
cat > config.json << CONF
{
  "username": "${PANEL_USER}",
  "password": "${PANEL_PASS}",
  "instances": [],
  "check_interval": 60,
  "fail_threshold": 3,
  "check_port": 22,
  "bark_url": ""
}
CONF

echo -e "${GREEN}[6/6] 创建系统服务...${NC}"
SECRET_KEY=$(openssl rand -hex 32)
cat > /etc/systemd/system/${SERVICE_NAME}.service << SVCEOF
[Unit]
Description=AWS.sb Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="PORT=${PORT}"
Environment="PANEL_USER=${PANEL_USER}"
Environment="PANEL_PASS=${PANEL_PASS}"
Environment="SECRET_KEY=${SECRET_KEY}"
ExecStart=${INSTALL_DIR}/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl restart $SERVICE_NAME

SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

echo -e "\n${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         安装完成！                       ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo -e ""
echo -e "  ${BLUE}面板地址:${NC}  http://${SERVER_IP}:${PORT}"
echo -e "  ${BLUE}用户名:${NC}    ${PANEL_USER}"
echo -e "  ${BLUE}密码:${NC}      ${PANEL_PASS}"
echo -e ""
echo -e "  常用命令:"
echo -e "  ${YELLOW}systemctl status ${SERVICE_NAME}${NC}   # 查看状态"
echo -e "  ${YELLOW}systemctl restart ${SERVICE_NAME}${NC}  # 重启服务"
echo -e "  ${YELLOW}journalctl -u ${SERVICE_NAME} -f${NC}   # 查看日志"
echo -e ""
