#!/bin/bash
set -e
[ -f /etc/nginx/aituner-upstream.conf ] || echo 'set $aituner_upstream http://127.0.0.1:8000;' > /etc/nginx/aituner-upstream.conf
F=/etc/nginx/sites-available/aituner
grep -q aituner-upstream.conf $F || {
  cp $F $F.bak
  sed -i 's|proxy_pass http://127.0.0.1:8000;|include /etc/nginx/aituner-upstream.conf; proxy_pass $aituner_upstream;|' $F
}
nginx -t && systemctl reload nginx
