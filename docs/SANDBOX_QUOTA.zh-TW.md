# Pi OJ 沙盒磁碟配額部署教學（Linux / Raspberry Pi 5）

這份教學提供給**自行架設 Pi OJ 的伺服器管理員**。一般使用網頁提交程式的人不需要設定。

## 保護範圍與限制

- 採用 Linux `tmpfs`，把 `backend/sandboxes` 掛成**有總容量上限**的暫存空間。
- 透過 `/etc/fstab` 設定，重新開機後 Linux 會自動掛載。
- Worker 會檢查容器內 `/sandboxes` 是否為**獨立的 tmpfs 掛載點**，且總容量沒有超出 `PI_OJ_SANDBOX_MAX_MIB`。不符合時，本機評測會回 `SE`，不執行使用者程式。
- **不是每人／每筆提交獨立的配額**。如果多人共用服務，一個惡意或失控的提交仍可能吃滿共享空間，讓其他提交失敗。
- `tmpfs` 會消耗 RAM 或 swap，請依 Raspberry Pi 的實際記憶體、編譯語言與工作量調整。建議先用 **512 MiB**，不代表任何硬體都適合這個數值。
- 這項保護**沒有解決 Docker Socket 的主機管理權限問題**，不適合拿來宣稱已完成公開多租戶沙盒隔離。

## 事前準備

1. 在**真正執行 Docker Engine 的 Linux 主機**操作。Windows WSL 若正在 `/mnt/c/...` 開發，不要在那裡執行安裝腳本；請在 Raspberry Pi 上安裝。
2. 已設定專案根目錄 `.env`，包括可用的 `HOST_SANDBOX_ROOT`、`PI_OJ_BASIC_USER`、`PI_OJ_BASIC_PASSWORD`。
3. 使用 `docker compose stop worker` 停止 Worker。確保沒有評測進行中。
4. 檢查 `backend/sandboxes` 中是否有舊檔，**先確認用途、備份必要資料，再人工清理**。不要在有任務執行時掛載，因為 mount 會遮住底下原有內容。
5. 主機需有 Linux util-linux 工具（`findmnt`、`mountpoint`）與 root (`sudo`) 權限。

## 安裝（每台 Docker 主機各做一次）

```bash
cd ~/pi_oj

# 確認真正的主機路徑
realpath backend/sandboxes

# 把 .env 裡的 HOST_SANDBOX_ROOT 設為上述絕對路徑
nano .env

# 確保 Worker 沒有執行評測
docker compose stop worker

# 備份 /etc/fstab、寫入永久掛載設定、立刻掛載並驗證
sudo bash scripts/manage_sandbox_quota.sh install 512M

# 必須顯示 tmpfs 且包含 fstab 的持久設定
bash scripts/check_sandbox_quota.sh

# 重新建立 Worker 讓新模組與環境變數生效
docker compose up -d --force-recreate worker

# 從 Worker 內確認 tmpfs 掛載已被看見，輸出以 bytes 為單位
docker compose exec worker python3 -c 'from sandbox_quota import check_sandbox_quota; print(check_sandbox_quota())'
```

如果你的 `.env` 是先前在 Raspberry Pi 建立的，一定要確認 `HOST_SANDBOX_ROOT` 指向**同一台 Raspberry Pi 上**的 `backend/sandboxes`，不是 WSL 或 Windows 的位置。

### 配置大小

- `install 512M`：全站同時使用的沙盒暫存資料總上限 512 MiB。
- 可改 `install 1G`，但同時要評估主機 RAM 與 `PI_OJ_SANDBOX_MAX_MIB` 是否足夠。
- `PI_OJ_SANDBOX_MAX_MIB=1024` 是 Worker 接受的最大 tmpfs 容量（MiB），並不是實際的掛載容量。實際容量由 `/etc/fstab` 設定。
- 因 tmpfs 有總容量而非單筆配額，現有單一 Celery Worker 並行數為 1 可降低同時佔用，但不能完全防止檔案耗盡或異常結束殘留。

## 重新開機驗證

```bash
sudo reboot
# 重新登入 Raspberry Pi 後
cd ~/pi_oj
bash scripts/check_sandbox_quota.sh
docker compose ps
```

即使掛載失敗，Worker 的本機評測入口仍預設**拒絕執行**。不要為了讓判題恢復而直接關閉保護，應先檢查掛載問題。

## 如果先前已執行暫時掛載腳本

`setup_sandbox_tmpfs.sh` 只做臨時 mount，不會自動寫入 `/etc/fstab`。先停 Worker、檢查與清理暫存區，然後人工 `sudo umount <實際 sandbox 絕對路徑>`，再執行永久安裝。若 tmpfs 中還有資料，請先判斷是否需要備份。

## 檢查

```bash
mountpoint backend/sandboxes
findmnt --mountpoint "$(realpath backend/sandboxes)"
df -h backend/sandboxes
bash scripts/check_sandbox_quota.sh
```

`findmnt` 必須顯示 `FSTYPE` 是 `tmpfs`；`/etc/fstab` 應有唯一一行以 `# PI_OJ_PERSISTENT_SANDBOX_QUOTA` 標記的設定。

## 移除／復原

移除永久掛載前，一定先停止 Worker，並檢查 tmpfs 中**沒有尚需保留的資料**：

```bash
cd ~/pi_oj
docker compose stop worker
sudo bash scripts/manage_sandbox_quota.sh remove
```

`remove` 會拒絕卸載非空的 tmpfs，以免誤刪還在使用的內容。安裝和移除會備份 `/etc/fstab` 到 `/etc/fstab.pi-oj-backup.<時間>`，必要時可以手動復原。

**移除後本機評測會拒絕執行**，直到重新安裝正確的 tmpfs。

## 常見狀況

| 現象 | 可能原因／處理 |
|---|---|
| `HOST_SANDBOX_ROOT differs` | `.env` 的主機絕對路徑跟專案的 `backend/sandboxes` 不一致 |
| `Worker is running` | 先 `docker compose stop worker`，等正在執行的任務結束 |
| `contains files` | 先查看是否為殘留沙盒資料，不要無腦刪除 |
| `not a mount point` | 掛載沒有成功，檢查 `/etc/fstab`、`findmnt`、系統日誌 |
| `Sandbox quota is not active` | Worker 沒看到獨立 tmpfs，或容量高於可接受上限；重建 Worker 後再驗證 |
| WSL `/mnt/c/` 路徑拒絕安裝 | 這是刻意的防呆；請在正式 Linux Docker 主機操作 |
| Java、Go 編譯寫入空間不足 | 檢查 RAM、tmpfs 使用率；適當增加共享容量並重啟 |

## 本機開發模式

若在完全離線、私有的 WSL 開發環境測試功能，而尚未設定 Linux tmpfs，可在自己的 `.env` **暫時**設定：

```dotenv
PI_OJ_REQUIRE_SANDBOX_TMPFS=0
```

這會取消「配額失效就拒絕評測」的保護。**不得在公開或不可信的程式提交環境使用**。正式主機需恢復 `1`。
