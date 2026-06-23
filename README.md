# Pi OJ MVP

目前提供：

- 瀏覽器內撰寫 C++17 / Python 3
- 自訂 stdin 與預期輸出
- Celery 非同步評測與結果輪詢
- Docker 隔離編譯／執行
- 使用自己的 UVA 帳號代理提交（實驗性）

## 在 Raspberry Pi 安裝

需求：64-bit Raspberry Pi OS、Docker、Docker Compose plugin。

```bash
cd ~/pi_oj
cp .env.example .env
mkdir -p backend/sandboxes
```

編輯 `.env`：

```dotenv
HOST_SANDBOX_ROOT=/home/ian/pi_oj/backend/sandboxes
UVA_USERNAME=你的帳號
UVA_PASSWORD=你的密碼
```

`HOST_SANDBOX_ROOT` 必須是樹莓派主機的絕對路徑。接著建立 image 並啟動：

```bash
docker compose build judge-image web worker
docker compose up -d redis web worker
docker compose ps
```

在同一區域網路開啟：

```text
http://樹莓派IP:8000
```

查看工作程序日誌：

```bash
docker compose logs -f worker
```

## ngrok login gate

The web service is bound to `127.0.0.1:8000` in `docker-compose.yml`, so it is not exposed directly on the LAN/public network. To share it through ngrok with a login gate, copy the example policy:

```bash
cp ngrok-policy.example.yaml ngrok-policy.yaml
```

`ngrok-policy.example.yaml` includes three selectable modes:

- Microsoft OAuth: default, restricted to `s15465465@mail.yzu.edu.tw`.
- Google OAuth: uncomment the Google block and replace `your-email@gmail.com`.
- Basic Auth: uncomment the Basic Auth block and replace the `"username:password"` string. The password must be at least 8 characters.

Use exactly one mode at a time. Keep `ngrok-policy.yaml` private if it contains real account rules or Basic Auth credentials.

Start Pi OJ:

```bash
docker compose up -d redis web worker
```

Start ngrok with the policy:

```bash
ngrok http 8000 --traffic-policy-file ngrok-policy.yaml
```

Visitors must pass the selected ngrok login policy before traffic reaches Pi OJ.
## API

### 執行本機測試

`POST /api/run`

```json
{
  "language": "cpp",
  "code": "#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b;}",
  "input_data": "2 3\n",
  "expected_output": "5\n"
}
```

### 提交 UVA

`POST /api/uva/submit`

```json
{
  "problem_id": "100",
  "language": "cpp",
  "code": "..."
}
```

兩個端點都會回傳 `task_id`，使用 `GET /api/tasks/{task_id}` 查詢結果。

## 安全設計

- API 容器沒有 Docker socket。
- 使用者程式不透過 shell 拼接，測資直接送進 stdin。
- judge 容器沒有網路、移除 Linux capabilities、啟用 `no-new-privileges`。
- 限制 128 MB、0.5 CPU、64 processes、唯讀 root filesystem。
- C++ 編譯限時 15 秒，程式執行限時 5 秒。
- 每次工作使用隨機 UUID 目錄，完成後刪除。

掛載 Docker socket 的 worker 等同具有主機高權限，因此不要把 Celery/Redis port 暴露到公網，也不要接受不可信任來源直接呼叫 worker。正式公開服務應把 judge worker 放在獨立機器或 VM。

## UVA 限制

UVA 沒有為這個用途提供穩定的公開提交 API。此版本預設由 worker 內的無頭 Chromium 完成登入，再把 session 交給提交程序；網站改版、驗證碼、Cloudflare 或語言代碼異動都可能令它失效。Chromium 只安裝在 worker image，不會增加 web 容器的負擔。

系統只會在收到可接受的 HTTP 回應後顯示 `SUBMITTED`，不會假造 `AC`。最終 verdict 目前請到 UVA 的 **My Submissions** 確認。若 UVA 表單已改版，請保留 worker log 以便更新 `UvaClient` adapter。

