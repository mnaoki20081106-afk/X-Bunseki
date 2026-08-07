#!/bin/bash
# codespace_login.sh
# 【GitHub Codespaces専用】iPadなどPC/Macが無い環境でもXにログインできるようにする。
#
# 仕組み:
#   仮想ディスプレイ(Xvfb)上でPlaywrightのブラウザを起動し、
#   その画面をVNC → noVNC経由でWebブラウザ(Safari)から見えるようにする。
#
# 使い方:
#   1. このリポジトリをGitHub Codespacesで開く
#   2. ターミナルで: bash codespace_login.sh
#   3. 表示されるURL(ポート6080)をブラウザで開く
#   4. noVNCの画面で「Connect」を押すと、Xのログイン画面が見える
#   5. ダミーアカウントでログイン
#   6. ログインできたら、このターミナルに戻ってEnterキーを押す
#   7. 自動的に session_base64.txt が生成される(GitHub Secretsに登録する値)

set -e

echo "=== 必要なパッケージをインストール中... ==="
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xvfb x11vnc novnc websockify > /dev/null

echo "=== Python依存関係をインストール中... ==="
pip install --quiet --user playwright
export PATH="$HOME/.local/bin:$PATH"
python3 -m playwright install chromium
python3 -m playwright install-deps chromium

echo "=== 仮想ディスプレイを起動中... ==="
Xvfb :99 -screen 0 1280x800x24 &
XVFB_PID=$!
export DISPLAY=:99
sleep 2

echo "=== VNCサーバーを起動中... ==="
x11vnc -display :99 -nopw -forever -shared -bg -quiet

echo "=== noVNC(ブラウザ経由でVNCを見るためのプロキシ)を起動中... ==="
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &
NOVNC_PID=$!
sleep 2

echo ""
echo "=============================================================="
echo "準備ができました。"
echo ""
echo "1. VS Code(Codespaces)の下部にある「PORTS」タブを開いてください"
echo "2. ポート 6080 の行にある地球儀アイコン(ブラウザで開く)をタップ"
echo "3. 開いたページの右上あたりにある「Connect」ボタンをタップ"
echo "4. 表示された画面の中でXのログイン画面が見えるので、"
echo "   ダミーアカウントでログインしてください"
echo "5. ログインが完了したら、このターミナルに戻って Enter を押してください"
echo "=============================================================="
echo ""

python3 login_helper.py

echo ""
echo "=== ログイン状態をbase64化しています... ==="
base64 -w0 storage_state.json > session_base64.txt
echo "完了: session_base64.txt を作成しました。"
echo "このファイルの中身をコピーして、GitHub Secretsの X_SESSION_STATE に登録してください。"
echo ""
echo "中身を表示します(この下の文字列をコピーしてください):"
echo "--------------------------------------------------------------"
cat session_base64.txt
echo ""
echo "--------------------------------------------------------------"

# 後片付け
kill $XVFB_PID $NOVNC_PID 2>/dev/null || true
