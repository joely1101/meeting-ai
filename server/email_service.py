import os
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def send_verification_email(to_email, user_name, verify_token):
    cfg = load_config()
    base_url = cfg.get("base_url", "https://tools.jumper.cloudns.ch/recordv2").rstrip("/")
    verify_link = f"{base_url}/#verify={verify_token}"

    subject = "【Hawkeye 智慧會議系統】請驗證並啟用您的帳號"
    
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 10px;">
      <h2 style="color: #2563eb; margin-bottom: 16px;">歡迎加入 Hawkeye AI 會議工作台</h2>
      <p>親愛的 {user_name} 您好：</p>
      <p>感謝您註冊 Hawkeye 智慧會議系統。為了維護企業資訊安全，請點選下方連結以啟用您的帳號：</p>
      <div style="margin: 28px 0; text-align: center;">
        <a href="{verify_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
          驗證並啟用帳號
        </a>
      </div>
      <p style="color: #64748b; font-size: 0.9em;">若上方按鈕無法點擊，您也可以複製以下連結至瀏覽器開啟：</p>
      <p style="word-break: break-all; color: #2563eb; font-size: 0.85em;">{verify_link}</p>
      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 24px 0;" />
      <p style="color: #94a3b8; font-size: 0.8em;">此郵件由 Hawkeye AI 會議系統自動發送，請勿直接回覆。</p>
    </div>
    """

    print(f"\n=======================================================")
    print(f"📧 [Email Verification Generated]")
    print(f"Recipient: {to_email}")
    print(f"Verify Link: {verify_link}")
    print(f"=======================================================\n")

    if cfg.get("smtp_enabled") and cfg.get("smtp_user") and cfg.get("smtp_password"):
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = cfg.get("smtp_from", cfg.get("smtp_user"))
            msg["To"] = to_email
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            server = smtplib.SMTP(cfg.get("smtp_server"), cfg.get("smtp_port", 587), timeout=10)
            server.starttls()
            server.login(cfg.get("smtp_user"), cfg.get("smtp_password"))
            server.sendmail(msg["From"], [to_email], msg.as_string())
            server.quit()
            print(f"✅ 郵件成功送出至 {to_email}")
            return True, verify_link
        except Exception as e:
            print(f"⚠️ SMTP 郵件發送失敗: {e}")
            return False, verify_link

    return True, verify_link

def send_meeting_summary_email(to_email, meeting_title, summary_text, transcript_text="", meeting_id=""):
    """發送 Teams 會議紀錄與 AI 摘要至指定電子郵件"""
    if not to_email or not to_email.strip():
        return False, "無收件人 Email"

    cfg = load_config()
    subject = f"【AI 會議紀錄】{meeting_title}"

    # 簡單將 Markdown 轉為 HTML 格式呈現
    html_summary = summary_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_summary = html_summary.replace("\n", "<br/>")

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 680px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 10px; background-color: #ffffff;">
      <div style="border-bottom: 2px solid #2563eb; padding-bottom: 12px; margin-bottom: 20px;">
        <span style="background-color: #dbeafe; color: #1d4ed8; font-size: 12px; font-weight: bold; padding: 4px 8px; border-radius: 4px; text-transform: uppercase;">Teams AI 會議報告</span>
        <h2 style="color: #0f172a; margin: 10px 0 4px 0; font-size: 20px;">{meeting_title}</h2>
        <p style="color: #64748b; margin: 0; font-size: 13px;">會議 ID: {meeting_id}</p>
      </div>

      <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; margin-bottom: 24px;">
        <h3 style="color: #1e293b; margin-top: 0; margin-bottom: 12px; font-size: 16px; border-left: 4px solid #3b82f6; padding-left: 8px;">📊 會議重點摘要與行動項目</h3>
        <div style="line-height: 1.7; color: #334155; font-size: 14px;">
          {html_summary}
        </div>
      </div>

      <div style="background-color: #f1f5f9; border-radius: 6px; padding: 12px 16px; margin-bottom: 20px;">
        <p style="margin: 0; font-size: 13px; color: #475569;">
          📎 <strong>完整語音逐字稿</strong> 已作為附件夾帶於此郵件中（.txt 格式），歡迎下載查閱。
        </p>
      </div>

      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;" />
      <p style="color: #94a3b8; font-size: 12px; margin: 0;">此郵件由 Hawkeye AI 會議自動撥入機器人生成並寄送。</p>
    </div>
    """

    print(f"\n=======================================================")
    print(f"📧 [Sending Meeting Summary Email]")
    print(f"To: {to_email}")
    print(f"Title: {meeting_title}")
    print(f"=======================================================\n")

    if cfg.get("smtp_enabled") and cfg.get("smtp_user") and cfg.get("smtp_password"):
        try:
            from email.mime.application import MIMEApplication
            msg = MIMEMultipart("mixed")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = cfg.get("smtp_from", cfg.get("smtp_user"))
            msg["To"] = to_email

            body_part = MIMEMultipart("alternative")
            body_part.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(body_part)

            # 夾帶逐字稿文字檔
            if transcript_text:
                txt_part = MIMEApplication(transcript_text.encode("utf-8"), Name=f"{meeting_title}_逐字稿.txt")
                txt_part["Content-Disposition"] = f'attachment; filename="{meeting_title}_逐字稿.txt"'
                msg.attach(txt_part)

            server = smtplib.SMTP(cfg.get("smtp_server"), cfg.get("smtp_port", 587), timeout=15)
            server.starttls()
            server.login(cfg.get("smtp_user"), cfg.get("smtp_password"))
            recipients = [e.strip() for e in to_email.split(",") if e.strip()]
            server.sendmail(msg["From"], recipients, msg.as_string())
            server.quit()
            print(f"✅ 會議紀錄郵件成功送出至 {to_email}")
            return True, "郵件送出成功"
        except Exception as e:
            print(f"⚠️ 會議紀錄 SMTP 郵件發送失敗: {e}")
            return False, str(e)

    return False, "SMTP 未啟用或設定不完整"
