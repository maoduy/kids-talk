# kids-talk — Gọi điện - Nhắn tin con

[Tiếng Việt](#tiếng-việt) · [English](#english)

---

## Tiếng Việt

**Gọi điện - Nhắn tin con** giúp bố mẹ nhắn, nói và gọi cho con đang ngồi máy tính. Tin nhắn hiện lên màn hình
của con và được **đọc to bằng giọng tiếng Việt**. Con trả lời bằng nút bấm, bằng chữ hoặc bằng giọng
nói. Bố mẹ dùng một ứng dụng web trên điện thoại. Không cần Telegram, Zalo hay tài khoản nào khác.

Miễn phí, mã nguồn mở (AGPL-3.0). Mỗi gia đình tự chạy relay trên tài khoản Cloudflare miễn phí của
mình, nên **dữ liệu không đi qua máy chủ của ai khác**.

### Làm được gì

| | |
|---|---|
| 💬 Nhắn tin | Hiện thông báo trên máy con, đọc to (giọng Piper `vi_VN-vais1000`), có nút trả lời nhanh |
| 🎙️ Ghi âm hai chiều | Bố mẹ gửi đoạn ghi âm; con nói trả lời sau tiếng bíp, máy tự dừng khi con im 2 giây |
| 🔁 Lặp lại | Phát đi phát lại mỗi 10 giây (tối đa 1 giờ) đến khi con bấm Dừng hoặc trả lời |
| 📞 Gọi thoại | WebRTC; "Gọi tự trả lời" để máy con tự nghe máy; con cũng gọi được bố mẹ |
| ✅ Việc cần làm | Giao việc có hạn, máy con nhắc khi tới hạn, con đánh dấu xong |
| 🖥️ Máy con | Đang mở ứng dụng gì, thời gian dùng máy hôm nay, chụp màn hình từ xa |
| 🔒 Khóa máy | Khóa tài khoản của con đến giờ hẹn hoặc đến khi bố mẹ mở; tự mở khi hết giờ kể cả lúc mất mạng |
| 👨‍👩‍👧‍👦 Nhiều con | Mỗi con một máy; nhắn/gọi một máy hoặc tất cả; Bố và Mẹ đăng nhập riêng (passkey) |

Máy con: **Ubuntu 24.04 (GNOME)** hoặc **Windows 11**. Bố mẹ: điện thoại bất kỳ có trình duyệt
(Android/iPhone; cài như ứng dụng PWA để nhận thông báo).

### Cách hoạt động

```
Điện thoại bố mẹ (PWA) ──HTTPS──▶ Relay (Cloudflare Worker + D1 + KV, của chính gia đình)
                                        ▲
                       long-poll 25 s   │ HTTPS
                                        │
                     Máy con: agent (dịch vụ quản trị) + cửa sổ của con
```

- **Relay** (`worker/`): Cloudflare Worker, cơ sở dữ liệu D1, KV cho ảnh màn hình và ghi âm,
  Web Push cho điện thoại. Nằm trong gói miễn phí của Cloudflare với một gia đình vài máy.
- **Agent máy con** chạy bằng quyền quản trị (con không tắt được), giữ một lượt chờ 25 giây tới relay
  và nhận tin ngay khi bố mẹ gửi.
  - Linux (`agent/`): dịch vụ systemd, tiện ích GNOME để đọc ứng dụng đang mở và chụp màn hình.
  - Windows (`agent-win/`): tác vụ SYSTEM + một tiến trình nhỏ trong phiên của con (thông báo, âm thanh, micro).
- **Cuộc gọi**: WebRTC, âm thanh mã hoá đầu-cuối (DTLS-SRTP). Nếu mạng của con chặn UDP, cần một máy
  chủ TURN (coturn của bạn, hoặc Cloudflare Realtime TURN).

### Mã hoá đầu-cuối và quyền riêng tư

Bật trong **Cài đặt → Mã hóa đầu-cuối**. Cả nhà dùng chung **một khóa gia đình** (AES-256-GCM). Khóa
chỉ nằm trên điện thoại bố mẹ (IndexedDB) và máy của con (file cấu hình chỉ quản trị đọc được), **không
bao giờ gửi lên relay**. Relay chỉ biết dấu vân tay của khóa (một mã băm) để báo khi hai điện thoại
dùng khóa khác nhau.

| Relay (và Cloudflare, hay ai lấy được cơ sở dữ liệu) **không đọc được** | Relay **vẫn thấy** (siêu dữ liệu) |
|---|---|
| Nội dung tin nhắn, ghi âm, tên việc | Ai gửi, lúc nào, dài bao nhiêu; đã tới / đã xem |
| Lời nhắn khi khóa máy | Máy đang khóa hay mở, khóa đến mấy giờ |
| Ứng dụng đang mở, tiêu đề cửa sổ, thời gian dùng máy | Máy online lúc nào; tên các máy |
| Ảnh màn hình | Có lệnh chụp, lúc nào |
| Âm thanh cuộc gọi (WebRTC DTLS-SRTP) và thông tin bắt tay cuộc gọi | Có cuộc gọi, ai gọi ai, bao lâu |

- Dữ liệu bị sửa trên đường đi thì bị phát hiện (GCM có xác thực). Khi đã có khóa, máy của con **bỏ qua**
  mọi tin "của bố mẹ" không mã hoá, và điện thoại **từ chối** cuộc gọi không mã hoá, nên relay không
  giả tin nhắn hay chen vào cuộc gọi được.
- **Giới hạn:**
  - Relay vẫn có thể khóa/mở máy hoặc xoá tin, vì trạng thái khóa không được ký.
  - Ai có khóa gia đình thì đọc được mọi tin. Hãy gửi link mời có kèm khóa qua kênh riêng, và cất một bản khóa ở nơi an toàn.
  - Mất hết các bản khóa thì không đọc lại được tin cũ.
  - Tin cũ gửi trước khi bật mã hoá vẫn ở dạng đọc được, và có dấu 🔓 trong app.
- Mọi điện thoại và máy con phải có cùng khóa:
  - Mẹ: mở link mời (có khóa), hoặc dán khóa trong Cài đặt.
  - Máy con: nhận khóa qua mã cài trong "Thêm máy của con".
  - iPhone: ứng dụng ở màn hình chính có bộ nhớ riêng, nên phải dán khóa trong Cài đặt của ứng dụng đó.
- Phần mềm này dành cho **bố mẹ và con chưa thành niên**. Máy con luôn hiện rõ biểu tượng "Gọi điện - Nhắn tin con"
  và thông báo khi bị khóa; không có chế độ ẩn và sẽ không có. Hãy nói với con rằng máy có cài phần mềm này.
  Không dùng để theo dõi người lớn.

### Cài đặt

Cần: một tài khoản Cloudflare (miễn phí), Node.js 20+ trên máy của bạn, quyền quản trị trên máy con.
Con phải dùng **tài khoản thường** (không phải quản trị) thì khóa máy mới có tác dụng.

**1. Relay**

```bash
git clone https://github.com/maoduy/kids-talk && cd kids-talk/worker
npm install
npx wrangler login
npx wrangler d1 create kids-talk            # ghi lại database_id
npx wrangler kv namespace create kids-talk  # ghi lại id
cp wrangler.toml.example wrangler.toml      # điền 2 id ở trên
npx wrangler d1 execute kids-talk --remote --file schema.sql
node gen-secrets.mjs --json > secrets.json  # giữ kín; SETUP_CODE dùng ở bước 2
npx wrangler secret bulk secrets.json
npx wrangler deploy                         # in ra https://kids-talk.<tên>.workers.dev
```

**2. Điện thoại bố mẹ**: mở địa chỉ relay, nhập `SETUP_CODE`, tạo passkey (vân tay/khuôn mặt).
Thêm vào màn hình chính để nhận thông báo. Mẹ: Bố tạo lời mời trong ứng dụng.

**3. Bật mã hoá và thêm máy của con**:
1. Trong app, vào Cài đặt → **Bật mã hóa**.
2. Vào **Thêm máy của con**, nhập tên con. App in ra lệnh cài có sẵn token và khóa gia đình.
   Không muốn mã hoá thì dùng `worker/add-device.sh` thay cho bước này.

**4a. Máy con Ubuntu 24.04** (cần Google Chrome cho cửa sổ gọi):

```bash
cd agent && sudo ./install.sh https://kids-talk.<tên>.workers.dev <token> <tài-khoản-của-con> <khóa-gia-đình>
```

Con đăng xuất rồi đăng nhập lại một lần để tiện ích GNOME chạy.

**4b. Máy con Windows 11** (PowerShell quản trị). Bộ cài chưa tự cài phần chạy kèm, cần làm trước:

1. Cài [Microsoft Visual C++ Redistributable x64](https://aka.ms/vs/17/release/vc_redist.x64.exe).
2. Cài Python 3.12 vào `C:\Program Files\BoNhan\Python` (cho mọi người dùng), rồi
   `"C:\Program Files\BoNhan\Python\python.exe" -m pip install piper-tts onnxruntime numpy pillow sounddevice cryptography`.
3. Tải vào `C:\ProgramData\BoNhan\voices\`:
   [`vi_VN-vais1000-medium.onnx` + `.onnx.json`](https://huggingface.co/rhasspy/piper-voices/tree/main/vi/vi_VN/vais1000/medium)
   và [`silero_vad.onnx`](https://github.com/snakers4/silero-vad/tree/master/src/silero_vad/data).

```powershell
Set-ExecutionPolicy -Scope Process Bypass
cd agent-win
.\install.ps1 -Url https://kids-talk.<tên>.workers.dev -Token <token> -Kid <tài-khoản-của-con> -Key <khóa-gia-đình>
```

Nên tắt chế độ ngủ khi cắm sạc, để bố mẹ luôn gọi được.

**Nếu cần mở khóa bằng tay** (đăng nhập tài khoản quản trị):
Linux `sudo passwd -u <tài-khoản-của-con>` · Windows `Get-LocalUser <tài-khoản-của-con> | Enable-LocalUser`.

### Tình trạng

Đang dùng hằng ngày trên 2 máy của một gia đình (một Ubuntu, một Windows). Chưa thử nhiều trên iPhone,
mạng di động, hoặc máy khác. Còn thiếu trên Windows: nghe máy khi con chưa đăng nhập, tạm dừng
nhạc/YouTube khi đọc tin, tự tăng âm lượng. Góp ý và sửa lỗi xin gửi qua Issues / Pull requests.

---

## English

**Gọi điện - Nhắn tin con** ("Call and message your child") lets parents text, talk to and call a child sitting at a computer.
Messages pop up on the child's screen and are **read aloud with a Vietnamese voice**; the child answers
with a button, text or voice. Parents use a web app on their phone: no Telegram, Zalo or other account.

Free and open source (AGPL-3.0). Every family runs its own relay on its own free Cloudflare account, so
**the data never passes through anyone else's server**. The interface is in Vietnamese.

### Features

| | |
|---|---|
| 💬 Messages | Notification on the child's computer, read aloud (Piper voice `vi_VN-vais1000`), quick replies |
| 🎙️ Voice clips both ways | Parents send a recording; the child answers after a beep, recording stops after 2 s of silence |
| 🔁 Repeat | Plays every 10 s (up to 1 h) until the child presses Stop or answers |
| 📞 Voice calls | WebRTC; "auto-answer" calls; the child can call the parents too |
| ✅ Tasks | Tasks with due times, reminders on the computer, the child ticks them off |
| 🖥️ Computer | Current app, today's screen time, remote screenshot |
| 🔒 Hard lock | Disables the child's account until a time or until unlocked; unlocks on time even offline |
| 👨‍👩‍👧‍👦 Several children | One computer per child; message/call one or all; Dad and Mom sign in separately (passkeys) |

Child's computer: **Ubuntu 24.04 (GNOME)** or **Windows 11**. Parents: any phone browser
(Android/iPhone; install it as a PWA for notifications).

### How it works

```
Parent's phone (PWA) ──HTTPS──▶ Relay (Cloudflare Worker + D1 + KV, owned by the family)
                                       ▲
                      long-poll 25 s   │ HTTPS
                                       │
                 Child's computer: agent (admin service) + the child's window
```

- **Relay** (`worker/`): Cloudflare Worker, D1 database, KV for screenshots and voice clips, Web Push.
  A family with a few computers fits in Cloudflare's free plan.
- **Agent** runs with admin rights (the child cannot stop it), holds a 25-second long-poll to the relay
  and gets messages as soon as they are sent.
  - Linux (`agent/`): systemd service plus a GNOME Shell extension for the focused app and screenshots.
  - Windows (`agent-win/`): a SYSTEM scheduled task plus a small helper in the child's session
    (popups, sound, microphone).
- **Calls**: WebRTC, audio encrypted end to end (DTLS-SRTP). If the child's network blocks UDP you need
  a TURN server (your own coturn, or Cloudflare Realtime TURN).

### End-to-end encryption and privacy

Turn it on in **Settings → Mã hóa đầu-cuối**. The family shares **one family key** (AES-256-GCM). It
lives only on the parents' phones (IndexedDB) and the children's computers (admin-only config file) and
is **never sent to the relay**. The relay only stores a fingerprint (a hash) so it can tell when two
phones hold different keys.

| The relay (and Cloudflare, or anyone with the database) **cannot read** | The relay **still sees** (metadata) |
|---|---|
| Message text, voice clips, task titles | Who sent what when, sizes; delivered / read |
| The lock message | Whether a computer is locked, and until when |
| Focused app, window titles, screen time | When a computer is online; computer names |
| Screenshots | That a screenshot was asked for, and when |
| Call audio (WebRTC DTLS-SRTP) and the call handshake (SDP) | That a call happened, between whom, how long |

- Tampering is detected (GCM is authenticated). Once a key is set, the child's computer **ignores** any
  unencrypted "parent" message and the phone **refuses** unencrypted call handshakes, so the relay
  cannot fake messages or sit in the middle of a call.
- **Limits:**
  - The relay can still lock/unlock a computer or drop messages, because the lock state is not signed.
  - Whoever holds the family key reads everything. Send the invite link (it carries the key) privately, and keep a backup of the key.
  - Losing every copy of the key means old messages cannot be read again.
  - Messages sent before encryption was turned on stay readable and are marked 🔓 in the app.
- Every phone and computer needs the same key:
  - Mom: open the invite link (it carries the key), or paste the key in Settings.
  - Children's computers: they get the key through the install command from "Thêm máy của con".
  - iPhone: a home-screen app has its own storage, so paste the key in that app's Settings.
- This is for **parents and their underage children**. The child's computer always shows the
  "Gọi điện - Nhắn tin con" icon and tells the child when it is locked; there is no hidden mode and there won't be one.
  Tell your child it is installed. Do not use it to watch adults.

### Setup

You need a free Cloudflare account, Node.js 20+ on your own computer, and admin rights on the child's
computer. The child must use a **standard (non-admin) account** for the lock to work.

**1. Relay**

```bash
git clone https://github.com/maoduy/kids-talk && cd kids-talk/worker
npm install
npx wrangler login
npx wrangler d1 create kids-talk            # note the database_id
npx wrangler kv namespace create kids-talk  # note the id
cp wrangler.toml.example wrangler.toml      # fill in both ids
npx wrangler d1 execute kids-talk --remote --file schema.sql
node gen-secrets.mjs --json > secrets.json  # keep it private; SETUP_CODE is used in step 2
npx wrangler secret bulk secrets.json
npx wrangler deploy                         # prints https://kids-talk.<name>.workers.dev
```

**2. Parent's phone**: open the relay address, enter `SETUP_CODE`, create a passkey. Add it to the
home screen for notifications. For Mom: create an invite inside the app.

**3. Turn on encryption and add each child's computer**:
1. In the app, open Settings → **Bật mã hóa**.
2. Open **Thêm máy của con** and type the child's name. The app prints the install command with the token and the family key.
   Without encryption, use `worker/add-device.sh` instead.

**4a. Ubuntu 24.04** (needs Google Chrome for the call window):

```bash
cd agent && sudo ./install.sh https://kids-talk.<name>.workers.dev <token> <child-account> <family-key>
```

The child logs out and in once so the GNOME extension loads.

**4b. Windows 11** (admin PowerShell). The installer does not install its runtime yet; first:

1. Install the [Microsoft Visual C++ Redistributable x64](https://aka.ms/vs/17/release/vc_redist.x64.exe).
2. Install Python 3.12 into `C:\Program Files\BoNhan\Python` (for all users), then
   `"C:\Program Files\BoNhan\Python\python.exe" -m pip install piper-tts onnxruntime numpy pillow sounddevice cryptography`.
3. Download into `C:\ProgramData\BoNhan\voices\`:
   [`vi_VN-vais1000-medium.onnx` + `.onnx.json`](https://huggingface.co/rhasspy/piper-voices/tree/main/vi/vi_VN/vais1000/medium)
   and [`silero_vad.onnx`](https://github.com/snakers4/silero-vad/tree/master/src/silero_vad/data).

```powershell
Set-ExecutionPolicy -Scope Process Bypass
cd agent-win
.\install.ps1 -Url https://kids-talk.<name>.workers.dev -Token <token> -Kid <child-account> -Key <family-key>
```

Turn off sleep while plugged in, so parents can always reach the computer.

**Manual unlock** (from an admin account):
Linux `sudo passwd -u <child-account>` · Windows `Get-LocalUser <child-account> | Enable-LocalUser`.

### Status

In daily use on one family's two computers (one Ubuntu, one Windows). Not tested much on iPhones,
mobile data, or other hardware. Missing on Windows: answering a call before the child logs in, pausing
music/YouTube while a message is read, automatic volume boost. Issues and pull requests are welcome.

## License

[AGPL-3.0](LICENSE). Voice model: Piper `vi_VN-vais1000-medium` (see its own licence on Hugging Face).
