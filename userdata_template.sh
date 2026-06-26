#!/bin/bash
# AWS 实例开机脚本模板
# 只需修改下面两个配置项

# ===== 需要修改的配置 =====
PANEL_URL="http://YOUR_SERVER_IP:5000"   # 国内面板地址
REPORT_KEY="YOUR_REPORT_KEY"             # 面板设置里的上报密钥
# ==========================

# 等待网络就绪
sleep 30

# 自动获取 Instance ID 和公网 IP
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
PUBLIC_IP=$(curl -s -4 ifconfig.me || curl -s ipv4.icanhazip.com)
IPV6=$(curl -s -6 ifconfig.me 2>/dev/null || echo "")

# 推送到面板
curl -s -X POST "${PANEL_URL}/api/report-ip" \
  -H "Content-Type: application/json" \
  -H "X-Access-Key: ${REPORT_KEY}" \
  -d "{\"instance_id\": \"${INSTANCE_ID}\", \"ip\": \"${PUBLIC_IP}\", \"ipv6\": \"${IPV6}\"}"

echo "IP 推送完成: $PUBLIC_IP / $IPV6"
