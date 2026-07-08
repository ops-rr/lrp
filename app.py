import hashlib
import requests
import json
import firebase_admin
from firebase_admin import credentials, db
from typing import Dict, Any, List
import jwt
import os
import time
from flask import Flask, request, jsonify
import gspread
from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials
from authlib.integrations.flask_client import OAuth
from flask import session, redirect, url_for, request as flask_request

# settings
SIGNING_SECRET = os.environ.get("SEATALK_BOT_SECRETSIGNING", "").encode()

# event list
EVENT_VERIFICATION = "event_verification"
NEW_BOT_SUBSCRIBER = "new_bot_subscriber"
MESSAGE_FROM_BOT_SUBSCRIBER = "message_from_bot_subscriber"
INTERACTIVE_MESSAGE_CLICK = "interactive_message_click"
BOT_ADDED_TO_GROUP_CHAT = "bot_added_to_group_chat"
BOT_REMOVED_FROM_GROUP_CHAT = "bot_removed_from_group_chat"
NEW_MENTIONED_MESSAGE_RECEIVED_FROM_GROUP_CHAT = (
    "new_mentioned_message_received_from_group_chat"
)

app = Flask(__name__)
# OAuth 設定（加在 app = Flask(__name__) 後面）
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


# =====================
# Firebase 初始化（維持原本邏輯）
# =====================
def is_valid_signature(signing_secret: bytes, body: bytes, signature: str) -> bool:
    calculated_signature = hashlib.sha256(body + signing_secret).hexdigest()
    return calculated_signature == signature


def init_firebase():
    service_account_json_str = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not service_account_json_str:
        raise ValueError("環境變數 'FIREBASE_SERVICE_ACCOUNT_JSON' 未設定。")
    service_account_info = json.loads(service_account_json_str)
    cred = credentials.Certificate(service_account_info)
    db_url = os.environ.get("FIREBASE_DATABASE_URL")
    if not db_url:
        raise ValueError("環境變數 'FIREBASE_DATABASE_URL' 未設定。")
    firebase_admin.initialize_app(cred, {"databaseURL": db_url})
    print("Firebase Admin SDK 初始化成功。")


init_firebase()


# =====================
# SeaTalk 共用函式
# =====================
def get_access_token() -> str:
    app_id = os.environ.get("SEATALK_BOT_APP_ID")
    app_secret = os.environ.get("SEATALK_BOT_APP_SECRET")
    response = requests.post(
        "https://openapi.seatalk.io/auth/app_access_token",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    return response.json().get("app_access_token")


def get_employee_code(email: str, access_token: str) -> str:
    response = requests.get(
        f"https://openapi.seatalk.io/contacts/v2/user/info?email={email}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    data = response.json()
    print(f"get_employee_code response: {data}")
    return data.get("employee_code")


def build_leave_card(leave_data: dict) -> dict:
    return {
        "elements": [
            {"element_type": "title", "title": {"text": f"{leave_data['employee_name']}請假審核申請"}},
            {
                "element_type": "description",
                "description": {
                    "format": 1,
                    "text": f"**假別**：{leave_data['leave_type']}\n**開始時間**：{leave_data['start_datetime']}\n**結束時間**：{leave_data['end_datetime']}",
                },
            },
            {
                "element_type": "button",
                "button": {
                    "button_type": "callback",
                    "text": "審核通過",
                    "value": json.dumps(
                        {
                            "action": "approve",
                            "reason": "審核通過",
                            "request_id": leave_data["request_id"],
                        }
                    ),
                },
            },
        ]
    }


def send_leave_request_card(group_id,leave_data: dict):
    """發送請假審核互動卡片到指定群組 (透過 Bot API，支援按鈕點擊)"""
    
    # 1. 取得 Bot 的 Access Token
    access_token = get_access_token()
    
    # 2. 填入你剛剛抓到的群組 ID
    TARGET_GROUP_ID = group_id

    # 3. 依照官方文件，針對 群組 Bot API (/v2/group_chat) 的 Payload 格式
    payload = {
        "group_id": TARGET_GROUP_ID,
        "message": {
            "tag": "interactive_message",
            "interactive_message": build_leave_card(leave_data),
        }
    }

    # 4. 改用 Bot 群組發送網址
    url = "https://openapi.seatalk.io/messaging/v2/group_chat"

    # 5. 發送 Request (必須帶 Authorization Header)
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
    )

    result = response.json()
    print(f"send_leave_request_card response: {result}")
    return result


# =====================
# Google Sheets 寫入（審核完成後記錄）
# =====================
def write_to_sheets(leave_data: dict, action: str, reason: str):
    """審核完成後寫一筆記錄到 Google Sheets"""
    try:
        service_account_info = json.loads(
            os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
        )
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        gc = gspread.authorize(creds)

        sheet_id = os.environ.get("LEAVE_SHEET_ID")
        sheet = gc.open_by_key(sheet_id).worksheet("休假記錄")

        sheet.append_row(
            [
                leave_data.get("created_at", ""),
                leave_data.get("employee_name", ""),
                leave_data.get("employee_email", ""),
                leave_data.get("leave_type", ""),
                leave_data.get("start_datetime", ""),
                leave_data.get("end_datetime", ""),
                leave_data.get("manager_email", ""),
                "approved" if action == "approve" else "rejected",
                reason,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ]
        )
        print("Google Sheets 寫入成功")
    except Exception as e:
        print(f"Google Sheets 寫入失敗: {e}")


def find_row_by_request_id(sheet, request_id: str):
    """在 Sheets 裡找到對應 request_id 的行號"""
    records = sheet.get_all_values()
    for i, row in enumerate(records):
        if row[0] == request_id:  # A欄是 request_id
            return i + 1  # gspread 行號從 1 開始
    return None


# ── Google OAuth ──────────────────────────────────────
@app.route("/auth/login")
def auth_login():
    redirect_uri = url_for("auth_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google.authorize_access_token()
    user = token.get("userinfo")
    email = user.get("email")
    name = user.get("name", email.split("@")[0])

    # 1. 準備要打包的資料 (Payload)
    # exp 是一個標準欄位，代表過期時間，這裡設定為 24 小時後過期
    payload = {
        "email": email,
        "name": name,
        "exp": datetime.now(timezone.utc) + timedelta(hours=24) 
    }

    # 2. 使用你的金鑰，把資料加密簽名成 Token
    token = jwt.encode(payload, SIGNING_SECRET, algorithm="HS256")

    # 4. 導回前端，現在改帶 token
    frontend_url = os.environ.get("FRONTEND_URL", "/leave")

    # 最終導向會變成：https://seatalk-callback.onrender.com/leave?token=xxxx...
    return redirect(f"{frontend_url}?token={token}")


@app.route("/leave")
def leave_page():
    with open("leave_app.html", "r", encoding="utf-8") as f:
        return f.read()


# ── 查詢申請記錄 ──────────────────────────────────────
@app.route("/leave/list", methods=["GET"])
def leave_list():
    # 1. 從前端送來的 Header 中抓取 Authorization 欄位
    auth_header = flask_request.headers.get('Authorization')
    
    # 確保格式正確
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({"status": "error", "message": "未授權，缺少 Token"}), 401

    # 切割字串，只拿出 Token 本體
    token = auth_header.split(" ")[1]

    try:
        # 2. 嘗試解密 Token
        decoded_data = jwt.decode(token, SIGNING_SECRET, algorithms=["HS256"])
        
        # 3. 解密成功，取得絕對可信的 email
        verified_email = decoded_data['email']

    except jwt.ExpiredSignatureError:
        return jsonify({"status": "error", "message": "登入狀態已過期，請重新登入"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"status": "error", "message": "無效的驗證碼，請勿竄改"}), 401

    # =====================================================================
    # Token 驗證通過後，才執行資料庫查詢
    # =====================================================================
    try:
        

        service_account_info = json.loads(
            os.environ.get("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON")
        )
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(os.environ.get("LEAVE_SHEET_ID")).worksheet("請假申請")

        records = sheet.get_all_records()
        
        # 【極度重要】：過濾時，強制使用 Token 解密出來的 verified_email
        user_records = [r for r in records if r.get("employee_email") == verified_email]

        return jsonify({"status": "ok", "records": user_records}), 200

    except Exception as e:
        print(f"leave_list error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# =====================
# 新 Route：接收請假申請
# =====================
@app.route("/leave/apply/test", methods=["POST"])

def leave_apply_test():
    try:
        leave_data = {
            "request_id": f"LEAVE_{int(time.time())}",
            "employee_email": "chris.chouyh@shopee.com",
            "employee_name": "Chris",
            "manager_email": "chris.chouyh@shopee.com",
            "leave_type": "特休",
            "start_datetime": "2026-05-01 09:00",
            "end_datetime": "2026-05-01 18:00",
            "reason": "家庭因素",
            "status": "pending",
            "reject_reason": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 寫入 Sheets
        service_account_info = json.loads(
            os.environ.get("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON")
        )
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(os.environ.get("LEAVE_SHEET_ID")).worksheet("請假申請")
        sheet.append_row(
            [
                leave_data["request_id"],
                leave_data["employee_email"],
                leave_data["employee_name"],
                leave_data["leave_type"],
                leave_data["start_datetime"],
                leave_data["end_datetime"],
                leave_data["reason"],
                leave_data["manager_email"],
                leave_data["status"],
                leave_data["reject_reason"],
                leave_data["created_at"],
            ]
        )

        # 發卡片
        access_token = get_access_token()
        bot_id = os.environ.get("SEATALK_BOT_ID")
        payload = {
            "employee_code": "247857",
            "message": {
                "tag": "interactive_message",
                "interactive_message": build_leave_card(leave_data),
            },
            "usable_platform": "mobile",
        }
        response = requests.post(
            "https://openapi.seatalk.io/messaging/v2/single_chat",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        result = response.json()
        print(f"send card result: {result}")
        return jsonify({"status": "ok", "seatalk_response": result}), 200

    except Exception as e:
        print(f"leave_apply_test error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/leave/apply", methods=["POST"])
def leave_apply():
    # 1. 從前端送來的 Header 中抓取 Authorization 欄位
    auth_header = request.headers.get('Authorization')
    
    # 確保格式正確
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({"status": "error", "message": "未授權，缺少 Token"}), 401

    # 切割字串，只拿出 Token 本體
    token = auth_header.split(" ")[1]

    try:
        # 2. 嘗試用同一把金鑰解密 Token
        decoded_data = jwt.decode(token, SIGNING_SECRET, algorithms=["HS256"])
        
        # 3. 恭喜！解密成功，提取出絕對可信的身分資料
        verified_email = decoded_data['email']
        verified_name = decoded_data['name']

    except jwt.ExpiredSignatureError:
        return jsonify({"status": "error", "message": "登入狀態已過期，請重新登入"}), 401
    
    except jwt.InvalidTokenError:
        return jsonify({"status": "error", "message": "無效的驗證碼，請勿竄改"}), 401

    # =====================================================================
    # 注意這裡的縮排！必須跟上面的 try/except 齊平，代表 Token 驗證通過後才執行
    # =====================================================================
    try:
        data = request.get_json()
        print(f"收到請假申請 (申請人: {verified_email}): {data}")

        # 用 AppSheet 傳來的 request_id，沒有的話才自己產生
        request_id = data.get("request_id") or f"LEAVE_{int(time.time())}"

        # 開啟 Sheets
        service_account_info = json.loads(
            os.environ.get("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON")
        )
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        gc = gspread.authorize(creds)

        # 查主管對應表 【修正：改用 verified_email 查詢】
        map_sheet = gc.open_by_key(os.environ.get("LEAVE_SHEET_ID")).worksheet("主管對應表")
        records = map_sheet.get_all_records()
        manager_data = next(
            (r for r in records if r["employee_email"] == verified_email), None
        )

        if not manager_data:
            print(f"找不到對應主管: {verified_email}")
            return jsonify({"status": "error", "message": "找不到對應主管"}), 400

        manager_email = manager_data["manager_email"]
        group_id = manager_data["group_id"]
        #manager_employee_code = str(manager_data["manager_employee_code"])
        department = manager_data.get("department", "未知部門")

        # 準備寫入資料 【修正：強制寫入 verified_email 與 verified_name】
        leave_data = {
            "request_id": request_id,
            "employee_department": department,  # <--- 絕對安全的來源
            "employee_email": verified_email,  # <--- 絕對安全的來源
            "employee_name": verified_name,    # <--- 絕對安全的來源
            "leave_type": data["leave_type"],
            "start_datetime": data["start_datetime"],
            "end_datetime": data["end_datetime"],
            "reason": data["reason"],
            "manager_email": manager_email,
            "status": "pending",
            "reject_reason": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 寫入 Google Sheets
        sheet = gc.open_by_key(os.environ.get("LEAVE_SHEET_ID")).worksheet("請假申請")
        sheet.append_row(
            [
                leave_data["request_id"],
                leave_data["employee_department"],
                leave_data["employee_email"],
                leave_data["employee_name"],
                leave_data["leave_type"],
                leave_data["start_datetime"],
                leave_data["end_datetime"],
                leave_data["reason"],
                leave_data["manager_email"],
                leave_data["status"],
                leave_data["reject_reason"],
                leave_data["created_at"],
            ]
        )

        # 發卡片給主管
        send_leave_request_card(group_id,leave_data)

        return jsonify({"status": "ok", "request_id": request_id}), 200

    except Exception as e:
        print(f"leave_apply error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# =====================
# 原本的 bot-callback（加上 INTERACTIVE_MESSAGE_CLICK 處理）
# =====================
@app.route("/bot-callback", methods=["POST"])
def bot_callback_handler():
    body: bytes = request.get_data()
    signature: str = request.headers.get("signature", "").strip()

    if not signature or not is_valid_signature(SIGNING_SECRET, body, signature):
        return "Invalid signature", 403

    try:
        data: Dict[str, Any] = json.loads(body)
        event_type: str = data.get("event_type", "")
        print(f"Received event type: {event_type}")

        seatalk_challenge = data.get("seatalk_challenge")
        if seatalk_challenge:
            return seatalk_challenge

        if event_type == EVENT_VERIFICATION:
            event_data = data.get("event", {})
            challenge = event_data.get("seatalk_challenge")
            if challenge:
                return jsonify({"seatalk_challenge": challenge})
            return "Verification event data not found.", 400

        elif event_type == INTERACTIVE_MESSAGE_CLICK:
            event_data = data.get("event", {})
            raw_value = event_data.get("value", "{}")
            click_data = json.loads(raw_value)

            action = click_data.get("action")
            request_id = click_data.get("request_id")
            reason = click_data.get("reason", "")

            # 取得該張卡片的 message_id
            message_id = event_data.get("message_id")

            print(
                f"互動點擊 - action: {action}, request_id: {request_id}, reason: {reason}"
            )

            if not request_id:
                return "", 200

            # 開啟 Sheets
            service_account_info = json.loads(
                os.environ.get("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON")
            )
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_info(
                service_account_info, scopes=scopes
            )
            gc = gspread.authorize(creds)
            sheet = gc.open_by_key(os.environ.get("LEAVE_SHEET_ID")).worksheet(
                "請假申請"
            )

            # 找到對應行
            row_num = find_row_by_request_id(sheet, request_id)
            if not row_num:
                print(f"找不到 request_id: {request_id}")
                return "", 200

            # 讀取這行的原始資料 (為了把名字、時間重畫在新卡片上)
            row_data = sheet.row_values(row_num)
            employee_department = row_data[1] if len(row_data) > 1 else "未知"
            employee_name = row_data[3] if len(row_data) > 3 else "未知"
            leave_type = row_data[4] if len(row_data) > 4 else "未知"
            start_dt = row_data[5] if len(row_data) > 5 else "未知"
            end_dt = row_data[6] if len(row_data) > 6 else "未知"

            # 更新 status 和 reject_reason (寫入 Google Sheets)
            sheet.update_cell(
                row_num, 10, "approved" if action == "approve" else "rejected"
            )  # I欄
            sheet.update_cell(row_num, 11, reason)  # J欄

            print(f"審核完成 - {request_id}: {action}")

            # ==========================================
            # 新增：動態更新原本的卡片 (把按鈕拔掉，換成結果文字)
            # ==========================================
            if message_id:
                if action == "approve":
                    status_text = "**已核准 (Approve)**"
                else:
                    status_text = f"**已拒絕** (原因：{reason})"

                # 建立一張「沒有按鈕」的新卡片
                updated_card = {
                    "elements": [
                        {
                            "element_type": "title",
                            "title": {
                                "text": f"{employee_name}請假審核申請 (已處理)"
                            }
                        },
                        {
                            "element_type": "description",
                            "description": {
                                "format": 1,
                                "text": f"**部門**：{employee_department}\n**員工**：{employee_name}\n**假別**：{leave_type}\n**開始時間**：{start_dt}\n**結束時間**：{end_dt}\n\n**審核結果**：{status_text}"
                            }
                        }
                    ]
                }

                access_token = get_access_token()
                
                # 🔴 依照官方文件，"message" 裡面直接放 "interactive_message"
                update_payload = {
                    "message_id": message_id,
                    "message": {
                        "interactive_message": updated_card
                    }
                }
                
                # 🔴 改用 POST，並指向 /v2/update 網址
                res = requests.post(
                    "https://openapi.seatalk.io/messaging/v2/update",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    json=update_payload
                )
                print(f"Update card response: {res.json()}")

            return "", 200

        # 以下維持原本邏輯不動
        elif event_type == NEW_BOT_SUBSCRIBER:
            print("New bot subscriber event received.")
            pass

        elif event_type == MESSAGE_FROM_BOT_SUBSCRIBER:
            print("Message from bot subscriber event received.")
            pass

        elif event_type == BOT_ADDED_TO_GROUP_CHAT:
            print("Bot added to group chat event received.")
            pass

        elif event_type == BOT_REMOVED_FROM_GROUP_CHAT:
            print("Bot removed from group chat event received.")
            pass

        elif event_type == NEW_MENTIONED_MESSAGE_RECEIVED_FROM_GROUP_CHAT:
            # Handle new mentioned message in group chat.
            # Example: Process the mention and respond to the user.
            group_id = data["event"]["group_id"]
            print(f"⭐⭐⭐ 抓到 group_id 了！這個群組的 ID 是: {group_id} ⭐⭐⭐")
            plain_text = data["event"]["message"]["text"]["plain_text"]
            thread_id = data["event"]["message"]["message_id"]
            print("New mentioned message in group chat received.")
            print(f"收到的CallBack內容:\n{data}")

            # 檢查訊息是否以 "@X10A" 開頭，並移除可能的換行或空格
            if plain_text.strip().startswith("(Production)"):
                # 使用 \n 分割字串，並過濾出指定開頭的行
                lines = [
                    line.strip()
                    for line in plain_text.split("\n")
                    if line.strip().startswith(("Order SN", "出貨失敗 TN"))
                ]

                data_dict = {}
                for line in lines:
                    if line.startswith("Order SN"):
                        data_dict["OSN"] = line.split("：", 1)[1]  # 取冒號後面的部分
                    elif line.startswith("出貨失敗 TN"):
                        data_dict["TN"] = line.split("：", 1)[1]

                new_data = [data_dict]
                add_err_order(new_data)

                # group_id = "NzMwNTUzMTAzMzg3"
                bot_reply("販賣機Err清單已更新成功", group_id, thread_id)

            elif plain_text.strip().startswith("Hi Team"):
                lines = [
                    line.strip()
                    for line in plain_text.split("\n")
                    if line.strip().startswith(
                        (
                            "Seller Type",
                            "Return Status",
                            "Return Reason",
                            "Seller Username",
                        )
                    )
                ]

                data_dict = {}

                for line in lines:
                    if line.startswith("Seller Type"):
                        data_dict["Seller Type"] = line.split("：", 1)[
                            1
                        ]  # 取冒號後面的部分

                    elif line.startswith("Return Status"):
                        normalized_line = line.replace("：", ":").strip()
                        data_dict["Return Status"] = normalized_line.split(":", 1)[
                            1
                        ].strip()

                    elif line.startswith("Return Reason"):
                        normalized_line = line.replace("：", ":").strip()
                        data_dict["Return Reason"] = normalized_line.split(":", 1)[
                            1
                        ].strip()

                    elif line.startswith("Seller Username"):
                        normalized_line = line.replace("：", ":").strip()
                        data_dict["Seller Username"] = normalized_line.split(":", 1)[
                            1
                        ].strip()

                special_seller_list = [
                    "fe_amart",
                    "digitalcitytw",
                    "senao.tw",
                    "daikin_senao",
                    "samsung_he",
                    "sakuyo_Japan",
                    "bianco_senao",
                    "MegaKing_senao",
                    "panasonic_senao",
                    "shopee_pass",
                    "esim_go",
                    "shopee24h",
                    "shopee24h_hb",
                    "shopee24h_el",
                    "asus_official_store",
                    "outsourcing_24h",
                    "thebestofkaohsiung",
                    "foodie.select",
                    "game_official",
                    "game_quick",
                    "google.tw",
                    "oppo_official",
                    "realmetw",
                    "shopee_consumables",
                    "sp_games",
                    "ticket_service",
                    "topbrandtw",
                    "shopee_choice_hl",
                    "apple.tw",
                    "asiawifi",
                ]
                flow = ""  # 初始化flow變數
                mention_tag = ""  # 初始化mention_tag變數

                print(f"解析後的資料字典: {data_dict}")
                if "Requested" in data_dict.get("Return Status", "").strip():

                    mention_tag = '<mention-tag target="seatalk://user?email=ziv.hung@shopee.com"/><mention-tag target="seatalk://user?email=sharon.chuic@shopee.com"/>'
                    content = f"{mention_tag}\n此案件需協助推送到Judging，請PIC協助確認案件內容!"
                    bot_reply(content, group_id, thread_id)

                elif (
                    "Judging" in data_dict.get("Return Status", "").strip()
                    or "Processing" in data_dict.get("Return Status", "").strip()
                ):
                    if (
                        data_dict.get("Seller Username", "").strip()
                        in special_seller_list
                    ):
                        flow = "Mall 特賣"
                        mention_tag = '<mention-tag target="seatalk://user?email=lynne.chung@shopee.com"/><mention-tag target="seatalk://user?email=vivian.liu@shopee.com"/>'
                        content = (
                            f"{mention_tag}\n此為{flow}案件，請PIC協助確認案件內容!"
                        )
                        bot_reply(content, group_id, thread_id)

                    else:
                        reason = data_dict.get("Return Reason", "").strip()
                        if "包裹未送達／無法取件" in reason:
                            flow = "Flow A"
                            mention_tag = '<mention-tag target="seatalk://user?email=vivian.liu@shopee.com"/><mention-tag target="seatalk://user?email=lynne.chung@shopee.com"/>'
                            content = (
                                f"{mention_tag}\n此為{flow}案件，請PIC協助確認案件內容!"
                            )
                            bot_reply(content, group_id, thread_id)

                        elif "商品缺件／賣家通知缺貨" in reason:
                            flow = "Flow B"
                            mention_tag = '<mention-tag target="seatalk://user?email=tina.tang@shopee.com"/><mention-tag target="seatalk://user?email=jennifer.su@shopee.com"/>'
                            content = (
                                f"{mention_tag}\n此為{flow}案件，請PIC協助確認案件內容!"
                            )
                            bot_reply(content, group_id, thread_id)
                        else:
                            flow = "Flow C"
                            mention_tag = '<mention-tag target="seatalk://user?email=sharon.chuic@shopee.com"/><mention-tag target="seatalk://user?email=ziv.hung@shopee.com"/>'
                            content = (
                                f"{mention_tag}\n此為{flow}案件，請PIC協助確認案件內容!"
                            )
                            bot_reply(content, group_id, thread_id)

                elif (
                    "Accepted" in data_dict.get("Return Status", "").strip()
                    or "Seller dispute" in data_dict.get("Return Status", "").strip()
                    or "Seller Dispute" in data_dict.get("Return Status", "").strip()
                ):

                    if "C2C" in data_dict.get("Seller Type", "").strip():
                        flow = "C2C Dispute"
                        mention_tag = '<mention-tag target="seatalk://user?email=alice.cheng@shopee.com"/><mention-tag target="seatalk://user?email=janice.lin@shopee.com"/>'
                        content = (
                            f"{mention_tag}\n此為{flow}案件，請PIC協助確認案件內容!"
                        )
                        bot_reply(content, group_id, thread_id)

                    elif "Mall" in data_dict.get("Seller Type", "").strip():
                        flow = "Mall Dispute"
                        mention_tag = '<mention-tag target="seatalk://user?email=shin.lee@shopee.com"/>'
                        # 移除Amelie，待後續加上
                        content = (
                            f"{mention_tag}\n此為{flow}案件，請PIC協助確認案件內容!"
                        )
                        bot_reply(content, group_id, thread_id)

                    elif "CB" in data_dict.get("Seller Type", "").strip():
                        flow = "CB Dispute"
                        mention_tag = '<mention-tag target="seatalk://user?email=queenie.chien@shopee.com"/><mention-tag target="seatalk://user?email=winnie.hsu@shopee.com"/>'
                        content = (
                            f"{mention_tag}\n此為{flow}案件，請PIC協助確認案件內容!"
                        )
                        bot_reply(content, group_id, thread_id)

                else:
                    content = f"Return Status內容有誤，無法判定通知對象，請重新確認格式"
                    bot_reply(content, group_id, thread_id)

            else:

                content = "訊息內容未以指定關鍵字開頭，請重新確認格式"
                bot_reply(content, group_id, thread_id)

        else:
            print(f"Unknown event type: {event_type}")

    except json.JSONDecodeError:
        return "Invalid JSON in request body", 400

    return "", 200


if __name__ == "__main__":
    app.run(debug=True)
