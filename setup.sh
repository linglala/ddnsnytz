#!/bin/bash
# ==================================================
# 一键部署脚本
# 使用前请先导出环境变量，或执行：
#   source config.env && sudo -E bash setup.sh
# ==================================================

# 确保以 root 权限运行
if [ "$EUID" -ne 0 ]; then
  echo "请使用 root 权限或 sudo 运行此脚本！"
  exit 1
fi

# ---- 读取配置（从环境变量） ----
: "${API_TOKEN:?错误：请先导出 API_TOKEN}"
: "${ZONE_ID:?错误：请先导出 ZONE_ID}"
: "${RECORD_NAME:?错误：请先导出 RECORD_NAME}"
: "${TG_BOT_TOKEN:?错误：请先导出 TG_BOT_TOKEN}"
: "${TG_CHAT_ID:?错误：请先导出 TG_CHAT_ID}"
TTL="${TTL:-60}"
PROXIED="${PROXIED:-false}"

DDNS_SCRIPT_PATH="/usr/local/bin/cf-ddns-dual.sh"
LOG_PATH="/var/log/cf-ddns-dual.log"
OLD_IP_FILE="/var/lib/cf-ddns-old-ip.txt"

echo "=========================================="

# ---- 步骤 0：设置时区为上海 ----
echo "步骤 0: 设置时区为 Asia/Shanghai..."
timedatectl set-timezone Asia/Shanghai 2>/dev/null \
  || ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
echo "当前时间：$(date)"

echo "------------------------------------------"

# ---- 步骤 1：检查并安装依赖 ----
echo "步骤 1: 检查并安装依赖工具..."

install_pkg() {
    local pkg=$1
    if command -v apt-get &>/dev/null; then
        apt-get install -y "$pkg" -qq
    elif command -v yum &>/dev/null; then
        yum install -y "$pkg" -q
    elif command -v dnf &>/dev/null; then
        dnf install -y "$pkg" -q
    elif command -v apk &>/dev/null; then
        apk add --quiet "$pkg"
    else
        echo "错误：无法识别包管理器，请手动安装 $pkg"
        exit 1
    fi
}

for tool in curl wget; do
    if ! command -v "$tool" &>/dev/null; then
        echo "未找到 $tool，正在安装..."
        install_pkg "$tool"
        command -v "$tool" &>/dev/null || { echo "错误：$tool 安装失败！"; exit 1; }
        echo "$tool 安装成功。"
    else
        echo "$tool 已存在，跳过。"
    fi
done

echo "------------------------------------------"

# ---- 步骤 2：生成 DDNS 核心脚本 ----
echo "步骤 2: 正在生成双栈 DDNS 核心脚本..."

cat << EOF > "$DDNS_SCRIPT_PATH"
#!/bin/bash
# ====================================================
# Cloudflare IPv4/IPv6 双栈 DDNS 独立核心脚本
# 由 setup.sh 自动生成，请勿手动编辑配置值
# ====================================================
API_TOKEN="$API_TOKEN"
ZONE_ID="$ZONE_ID"
RECORD_NAME="$RECORD_NAME"
TTL=$TTL
PROXIED=$PROXIED
TG_BOT_TOKEN="$TG_BOT_TOKEN"
TG_CHAT_ID="$TG_CHAT_ID"
OLD_IP_FILE="$OLD_IP_FILE"

send_tg_notify() {
    local msg=\$1
    local tg_resp
    tg_resp=\$(curl -s -X POST "https://api.telegram.org/bot\${TG_BOT_TOKEN}/sendMessage" \\
        -d chat_id="\${TG_CHAT_ID}" \\
        -d text="\${msg}" 2>&1)
    echo "\$(date): TG 推送结果: \$tg_resp"
}

update_dns_record() {
    local ip_type=\$1
    local current_ip=\$2

    [ -z "\$current_ip" ] && return

    local record_info
    record_info=\$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/\$ZONE_ID/dns_records?type=\$ip_type&name=\$RECORD_NAME" \\
         -H "Authorization: Bearer \$API_TOKEN" \\
         -H "Content-Type: application/json")

    local record_id old_ip
    record_id=\$(echo "\$record_info" | grep -o '"id":"[^"]*' | head -n 1 | grep -o '[^"]*\$')
    old_ip=\$(echo "\$record_info" | grep -o '"content":"[^"]*' | head -n 1 | grep -o '[^"]*\$')

    if [ -z "\$record_id" ]; then
        echo "\$(date): 未找到 \$RECORD_NAME 的 \$ip_type 记录，正在自动创建..."
        local create_result
        create_result=\$(curl -s -X POST "https://api.cloudflare.com/client/v4/zones/\$ZONE_ID/dns_records" \\
             -H "Authorization: Bearer \$API_TOKEN" \\
             -H "Content-Type: application/json" \\
             --data "{\\"type\\":\\"\$ip_type\\",\\"name\\":\\"\$RECORD_NAME\\",\\"content\\":\\"\$current_ip\\",\\"ttl\\":\$TTL,\\"proxied\\":\$PROXIED}")
        local success
        success=\$(echo "\$create_result" | grep -o '"success":[^,]*' | head -n 1 | grep -o '[a-z]*\$')
        [ "\$success" = "true" ] \\
            && echo "\$(date): 成功 - 已创建 \$ip_type 记录: \$current_ip" \\
            || echo "\$(date): 错误 - 创建 \$ip_type 记录失败！"
        return
    fi

    [ "\$current_ip" = "\$old_ip" ] && return

    echo "\$(date): \$ip_type 变化（\$old_ip -> \$current_ip），正在更新..."
    local update_result
    update_result=\$(curl -s -X PUT "https://api.cloudflare.com/client/v4/zones/\$ZONE_ID/dns_records/\$record_id" \\
         -H "Authorization: Bearer \$API_TOKEN" \\
         -H "Content-Type: application/json" \\
         --data "{\\"type\\":\\"\$ip_type\\",\\"name\\":\\"\$RECORD_NAME\\",\\"content\\":\\"\$current_ip\\",\\"ttl\\":\$TTL,\\"proxied\\":\$PROXIED}")
    local success
    success=\$(echo "\$update_result" | grep -o '"success":[^,]*' | head -n 1 | grep -o '[a-z]*\$')
    [ "\$success" = "true" ] \\
        && echo "\$(date): 成功 - \$ip_type 已更新为: \$current_ip" \\
        || echo "\$(date): 错误 - \$ip_type 更新失败。"
}

# ---- 获取公网 IP ----
CURRENT_IPV4=\$(curl -4 -s --max-time 10 https://api.ipify.org \\
    || curl -4 -s --max-time 10 https://ifconfig.me \\
    || curl -4 -s --max-time 10 https://ip.icanhazip.com)
CURRENT_IPV6=\$(curl -6 -s --max-time 10 https://api6.ipify.org \\
    || curl -6 -s --max-time 10 https://v6.ident.me)

if [ -z "\$CURRENT_IPV4" ] && [ -z "\$CURRENT_IPV6" ]; then
    echo "\$(date): 错误 - 无法获取任何公网 IP。"
    exit 1
fi

# ---- IP 变更通知 ----
if [ -n "\$CURRENT_IPV4" ]; then
    OLD_IP=\$(cat "\$OLD_IP_FILE" 2>/dev/null)
    if [ "\$CURRENT_IPV4" != "\$OLD_IP" ]; then
        if [ -n "\$OLD_IP" ]; then
            MSG="🔔 VPS IP 变更通知
域名: \$RECORD_NAME
旧 IP: \$OLD_IP
新 IP: \$CURRENT_IPV4
时间: \$(date)"
        else
            MSG="🟢 VPS DDNS 首次记录 IP
域名: \$RECORD_NAME
当前 IP: \$CURRENT_IPV4
时间: \$(date)"
        fi
        send_tg_notify "\$MSG"
        echo "\$CURRENT_IPV4" > "\$OLD_IP_FILE"
    fi
fi

# ---- 更新 DNS ----
[ -n "\$CURRENT_IPV4" ] && update_dns_record "A" "\$CURRENT_IPV4"
# [ -n "\$CURRENT_IPV6" ] && update_dns_record "AAAA" "\$CURRENT_IPV6"  # 如需 IPv6 取消注释
EOF

chmod +x "$DDNS_SCRIPT_PATH"
echo "成功：DDNS 脚本已生成 → $DDNS_SCRIPT_PATH"

echo "------------------------------------------"

# ---- 步骤 3：使用 systemd timer 替代 cron ----
echo "步骤 3: 设置 systemd timer 定时任务（每分钟）..."

cat > /etc/systemd/system/cf-ddns.service << 'UNIT'
[Unit]
Description=Cloudflare DDNS Update

[Service]
Type=oneshot
ExecStart=/usr/local/bin/cf-ddns-dual.sh
StandardOutput=append:/var/log/cf-ddns-dual.log
StandardError=append:/var/log/cf-ddns-dual.log
UNIT

cat > /etc/systemd/system/cf-ddns.timer << 'UNIT'
[Unit]
Description=Cloudflare DDNS Update Timer

[Timer]
OnBootSec=30
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now cf-ddns.timer
systemctl list-timers cf-ddns.timer --no-pager

echo "定时任务设置成功！"
echo "------------------------------------------"

# ---- 步骤 4：立即执行一次 DDNS ----
echo "步骤 4: 立即执行一次 DDNS 检测..."
bash "$DDNS_SCRIPT_PATH"

echo "------------------------------------------"

# ---- 步骤 5：部署 nyanpass ----
echo "步骤 5: 部署 nyanpass 节点客户端..."
printf '\n\n\n' | bash <(curl -fLSs https://dl.nyafw.com/download/nyanpass-install.sh) rel_nodeclient \
    "-t 5a067af7-6d14-4c43-bc11-cec264fd35b5 -u https://ny.128111.xyz"

echo "------------------------------------------"

# ---- 步骤 6：部署 komari-agent ----
echo "步骤 6: 部署 komari-agent 监控客户端..."
wget -qO- https://raw.githubusercontent.com/komari-monitor/komari-agent/refs/heads/main/install.sh \
    | bash -s -- -e https://tz.098757.xyz -t E4NxDKoH19Q1zlu0DPk2aH --disable-web-ssh

echo "=========================================="
echo "部署全部完成！"
echo "时区:       Asia/Shanghai（$(date '+%Z %z')）"
echo "DDNS 脚本:  $DDNS_SCRIPT_PATH"
echo "日志:       $LOG_PATH"
echo "定时方式:   systemd timer（每分钟）"
echo "Telegram:   已配置（IP 变更时推送）"
echo "=========================================="
