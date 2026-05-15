import os, psycopg2, pytesseract, json, time, smtplib
from psycopg2 import OperationalError
from PIL import Image, ImageDraw
import pika
from email.message import EmailMessage

UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
db_pass = os.getenv('DB_PASS')
db_url = os.getenv('DATABASE_URL') or f"host=ocr-db port=5444 dbname=ocrdb user=user password={db_pass}"


def log(message):
    print(message, flush=True)


def send_email_notification(filename, description, ocr_text):
    # send to all subscribers from the database
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT email FROM subscribers ORDER BY created_at ASC")
        subs = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
    except OperationalError as exc:
        log(f'Failed to load subscribers: {exc}')
        subs = []

    if not subs:
        log('No subscribers configured; skipping email send')
        return

    smtp_host = os.getenv('SMTP_HOST')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER')
    smtp_pass = os.getenv('SMTP_PASS')
    email_from = os.getenv('EMAIL_FROM', smtp_user or 'ocr@example.com')
    use_ssl = os.getenv('SMTP_USE_SSL', 'false').lower() == 'true'
    use_starttls = os.getenv('SMTP_USE_STARTTLS', 'true').lower() == 'true'

    if not smtp_host:
        log('SMTP_HOST not configured; skipping email')
        return

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    for recipient in subs:
        try:
            msg = EmailMessage()
            msg['Subject'] = f'OCR completed: {filename}'
            msg['From'] = email_from
            msg['To'] = recipient
            msg.set_content(
                f"OCR processing finished.\n\n"
                f"File: {filename}\n"
                f"Description: {description}\n"
                f"Detected text: {ocr_text or '(none)'}\n"
            )

            with smtp_class(smtp_host, smtp_port) as server:
                if use_starttls and not use_ssl:
                    server.starttls()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            log(f'Notification email sent to {recipient}')
        except Exception as exc:
            log(f'Failed to send notification email to {recipient}: {exc}')


def init_db():
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS uploads 
                   (id SERIAL PRIMARY KEY, filename TEXT, description TEXT, ocr_text TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS subscribers
                   (email TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT now())''')
        conn.commit()
        cur.close()
        conn.close()
    except OperationalError as exc:
        log(f'Database unavailable during worker startup: {exc}')


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
        log(f'Failed to save upload record: {exc}')


def process_task(body):
    try:
        task = json.loads(body)
        # If a recipient is provided, treat this as a notify-only message (catch-up)
        recipient = task.get('recipient')
        filename = task.get('filename')
        description = task.get('description', '')
        ocr_text = task.get('ocr_text')
        filepath = task.get('filepath')
        upload_folder = task.get('upload_folder', UPLOAD_FOLDER)

        if recipient:
            log(f'Notify-only task for {filename} -> {recipient}')
            # send single email for this file to the provided recipient
            try:
                smtp_host = os.getenv('SMTP_HOST')
                smtp_port = int(os.getenv('SMTP_PORT', '587'))
                smtp_user = os.getenv('SMTP_USER')
                smtp_pass = os.getenv('SMTP_PASS')
                email_from = os.getenv('EMAIL_FROM', smtp_user or 'ocr@example.com')
                use_ssl = os.getenv('SMTP_USE_SSL', 'false').lower() == 'true'
                use_starttls = os.getenv('SMTP_USE_STARTTLS', 'true').lower() == 'true'

                if not smtp_host:
                    log('SMTP_HOST not configured; skipping notify-only email')
                else:
                    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
                    msg = EmailMessage()
                    msg['Subject'] = f'OCR result: {filename}'
                    msg['From'] = email_from
                    msg['To'] = recipient
                    msg.set_content(f"File: {filename}\nDescription: {description}\n\nDetected text:\n{ocr_text or '(none)'}")
                    with smtp_class(smtp_host, smtp_port) as server:
                        if use_starttls and not use_ssl:
                            server.starttls()
                        if smtp_user and smtp_pass:
                            server.login(smtp_user, smtp_pass)
                        server.send_message(msg)
                    log(f'Catch-up notification sent to {recipient} for {filename}')
            except Exception as exc:
                log(f'Failed to send catch-up notification to {recipient}: {exc}')
            return

        # otherwise, process OCR task as normal
        log(f'Processing task for {filename}')

        # If no filepath is provided, assume this is a record-only notification (DB record exists)
        # and we already have `ocr_text` saved. In that case, send notifications to subscribers
        # instead of attempting to re-open the image file.
        if not filepath:
            if ocr_text:
                log(f'No filepath for {filename}; sending stored OCR text to subscribers')
                send_email_notification(filename, description, ocr_text)
            else:
                log(f'No filepath and no OCR text for {filename}; skipping')
            return

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
        log(f'Error processing task: {exc}')


def main():
    init_db()
    rabbit_host = os.getenv('RABBITMQ_HOST', 'rabbitmq')
    rabbit_user = os.getenv('RABBITMQ_USER')
    rabbit_pass = os.getenv('RABBITMQ_PASS')
    creds = None
    if rabbit_user and rabbit_pass:
        creds = pika.PlainCredentials(rabbit_user, rabbit_pass)
    params = pika.ConnectionParameters(host=rabbit_host, credentials=creds) if creds else pika.ConnectionParameters(host=rabbit_host)
    log(f'Worker starting. RabbitMQ host={rabbit_host}, queue=ocr_tasks')

    while True:
        try:
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue='ocr_tasks', durable=True)
            log('Connected to RabbitMQ and waiting for messages')

            for method, properties, body in channel.consume('ocr_tasks', inactivity_timeout=1):
                if body:
                    log('Received message from RabbitMQ')
                    process_task(body)
                    channel.basic_ack(method.delivery_tag)
                else:
                    # no message, continue loop
                    pass

        except Exception as exc:
            log(f'Worker connection error: {exc}')
            time.sleep(5)


if __name__ == '__main__':
    main()
