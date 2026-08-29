#!/bin/bash
set -e

# ============================================
# V2bX + Nginx 伪装站 一键部署脚本
# 适用：Cloudflare 自定义主机名 + VLESS WS TLS 后端
# ============================================

echo "========================================"
echo "  V2bX Nginx 伪装站一键部署"
echo "========================================"

# 交互式输入配置
echo ""
read -p "请输入 WebSocket Path [默认: /vless]: " WS_PATH
WS_PATH=${WS_PATH:-/vless}

read -p "请输入 V2bX 服务端口 [默认: 10086]: " V2BX_PORT
V2BX_PORT=${V2BX_PORT:-10086}

read -p "请输入回退源域名 [默认: cname.124333.xyz]: " DOMAIN
DOMAIN=${DOMAIN:-cname.124333.xyz}

echo ""
echo "配置确认:"
echo "  WS Path: $WS_PATH"
echo "  V2bX Port: $V2BX_PORT"
echo "  域名: $DOMAIN"
echo ""
read -p "确认开始部署? [Y/n]: " confirm
confirm=${confirm:-Y}
if [[ "$confirm" != "Y" && "$confirm" != "y" ]]; then
    echo "已取消"
    exit 0
fi

# 1. 安装 Nginx + 必要工具
echo ""
echo "[1/7] 安装 Nginx + 工具..."
apt-get install -y nginx unzip wget openssl >/dev/null 2>&1
systemctl start nginx
systemctl enable nginx >/dev/null 2>&1
echo "  完成"

# 2. 修复 Nginx hash bucket
echo "[2/7] 修复 Nginx hash bucket..."
sed -i 's/# server_names_hash_bucket_size 64;/server_names_hash_bucket_size 128;/' /etc/nginx/nginx.conf
grep -q "server_names_hash_bucket_size" /etc/nginx/nginx.conf || sed -i '/^http {/a \        server_names_hash_bucket_size 128;' /etc/nginx/nginx.conf
echo "  完成"

# 3. 下载伪装站
echo "[3/7] 下载伪装站模板..."
mkdir -p /var/www/camouflage
cd /var/www/camouflage
wget -q https://github.com/startbootstrap/startbootstrap-clean-blog/archive/refs/heads/gh-pages.zip -O blog.zip
unzip -q blog.zip
mv startbootstrap-clean-blog-gh-pages/* . 2>/dev/null || true
rm -rf blog.zip startbootstrap-clean-blog-gh-pages
chown -R www-data:www-data /var/www/camouflage
chmod -R 755 /var/www/camouflage
echo "  完成"

# 4. 临时自签名证书
echo "[4/7] 准备证书..."
if [ ! -f /etc/V2bX/fullchain.cer ]; then
    mkdir -p /etc/V2bX
    openssl req -x509 -nodes -days 1 -newkey rsa:2048 \
      -keyout /etc/V2bX/cert.key \
      -out /etc/V2bX/fullchain.cer \
      -subj "/CN=${DOMAIN}"
    echo "  已创建临时证书（V2bX 申请到正式证书后会自动覆盖）"
else
    echo "  已有证书，跳过"
fi

# 5. 写入 Nginx 配置
echo "[5/7] 写入 Nginx 配置..."
cat > /etc/nginx/conf.d/camouflage.conf << EOF
server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    ssl_certificate     /etc/V2bX/fullchain.cer;
    ssl_certificate_key /etc/V2bX/cert.key;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers on;

    root /var/www/camouflage;
    index index.html;

    location / {
        try_files \$uri \$uri/ =404;
    }

    location ${WS_PATH} {
        proxy_redirect off;
        proxy_pass https://127.0.0.1:${V2BX_PORT};
        proxy_ssl_verify off;

        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;

        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
        proxy_buffering off;
    }
}

server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$server_name\$request_uri;
}
EOF
echo "  完成"

# 6. 防火墙放行
echo "[6/7] 防火墙放行..."
ufw allow 80/tcp 2>/dev/null || true
ufw allow 443/tcp 2>/dev/null || true
ufw allow 443/udp 2>/dev/null || true
echo "  完成"

# 7. 测试并重载 Nginx
echo "[7/7] 测试并重载 Nginx..."
nginx -t && nginx -s reload
echo "  完成"

# 验证
echo ""
echo "========================================"
echo "  部署完成！"
echo "========================================"
echo ""
echo "验证命令:"
echo "  伪装站: curl -k https://127.0.0.1/ | head -5"
echo "  WS路径: curl -k -I https://127.0.0.1${WS_PATH}"
echo "  端口:   ss -tlnp | grep ':443'"
echo "  证书:   openssl x509 -in /etc/V2bX/fullchain.cer -noout -subject"
echo ""
echo "========================================"
