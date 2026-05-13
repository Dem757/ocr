import os, psycopg2, pytesseract, json, time, smtplib
from psycopg2 import OperationalError
from PIL import Image, ImageDraw
import pika
from email.message import EmailMessage

UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
db_pass = os.getenv('DB_PASS')
db_url = os.getenv('DATABASE_URL') or f"host=ocr-db port=5444 dbname=ocrdb user=user password={db_pass}"


def send_email_notification(filename, description, ocr_text):
    recipient = os.getenv('EMAIL_TO')
    smtp_host = os.getenv('SMTP_HOST')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER')
    smtp_pass = os.getenv('SMTP_PASS')
    email_from = os.getenv('EMAIL_FROM', smtp_user or 'ocr@example.com')

    if not recipient or not smtp_host:
        print('Email notification skipped: EMAIL_TO or SMTP_HOST is not configured')
        return

    message = EmailMessage()
    message['Subject'] = f'OCR completed: {filename}'
    message['From'] = email_from
    message['To'] = recipient
    message.set_content(
        f"OCR processing finished.\n\n"
        f"File: {filename}\n"
        f"Description: {description}\n"
        f"Detected text: {ocr_text or '(none)'}\n"
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        print(f'Notification email sent to {recipient}')
    except Exception as exc:
        print(f'Failed to send notification email: {exc}')


def init_db():
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS uploads 
                       (id SERIAL PRIMARY KEY, filename TEXT, description TEXT, ocr_text TEXT)''')
        conn.commit()
        cur.close()
        conn.close()
    except OperationalError as exc:
        print(f'Database unavailable during worker startup: {exc}')


def save_upload_record(filename, description, ocr_text):
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("INSERT INTO uploads (filename, description, ocr_text) VALUES (%s, %s, %s)",
                    (filename, description, ocr_text))
        conn.commit()
        cur.close()
        conn.close()
    except OperationalError as exc:
        print(f'Failed to save upload record: {exc}')


def process_task(body):
    try:
        task = json.loads(body)
        filename = task.get('filename')
        filepath = task.get('filepath')
        description = task.get('description', '')
        upload_folder = task.get('upload_folder', UPLOAD_FOLDER)

        img = Image.open(filepath)
        d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        draw = ImageDraw.Draw(img)

        full_text = []
        for i in range(len(d['text'])):
            try:
                if int(d['conf'][i]) > 60:
                    (x, y, w, h) = (d['left'][i], d['top'][i], d['width'][i], d['height'][i])
                    draw.rectangle([x, y, x + w, y + h], outline="red", width=3)
                    full_text.append(d['text'][i])
            except Exception:
                continue

        proc_filename = "proc_" + filename
        img.save(os.path.join(upload_folder, proc_filename))
        ocr_text = " ".join(full_text)
        save_upload_record(proc_filename, description, ocr_text)
        send_email_notification(proc_filename, description, ocr_text)
    except Exception as exc:
        print(f'Error processing task: {exc}')


def main():
    init_db()
    rabbit_host = os.getenv('RABBITMQ_HOST', 'rabbitmq')
    rabbit_user = os.getenv('RABBITMQ_USER')
    rabbit_pass = os.getenv('RABBITMQ_PASS')
    creds = None
    if rabbit_user and rabbit_pass:
        creds = pika.PlainCredentials(rabbit_user, rabbit_pass)
    params = pika.ConnectionParameters(host=rabbit_host, credentials=creds) if creds else pika.ConnectionParameters(host=rabbit_host)

    while True:
        try:
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue='ocr_tasks', durable=True)

            for method, properties, body in channel.consume('ocr_tasks', inactivity_timeout=1):
                if body:
                    process_task(body)
                    channel.basic_ack(method.delivery_tag)
                else:
                    # no message, continue loop
                    pass

        except Exception as exc:
            print(f'Worker connection error: {exc}')
            time.sleep(5)


if __name__ == '__main__':
    main()
