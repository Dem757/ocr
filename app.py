import os, psycopg2, pytesseract, json
import pika
from psycopg2 import OperationalError
from flask import Flask, render_template, request, redirect, send_from_directory
from PIL import Image, ImageDraw

app = Flask(__name__)
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
db_pass = os.getenv('DB_PASS')
db_url = os.getenv('DATABASE_URL') or f"host=ocr-db port=5444 dbname=ocrdb user=user password={db_pass}"
db_ready = False


def log(message):
    print(message, flush=True)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Adatbázis inicializálás [cite: 92]
def init_db():
    global db_ready

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
        db_ready = True
    except OperationalError as exc:
        db_ready = False
        log(f'Database unavailable during startup: {exc}')


def save_upload_record(filename, description, ocr_text):
    if not db_ready:
        return

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


def delete_upload_record(filename):
    if not db_ready:
        return

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("DELETE FROM uploads WHERE filename = %s", (filename,))
        conn.commit()
        cur.close()
        conn.close()
    except OperationalError as exc:
        log(f'Failed to delete upload record: {exc}')


def delete_upload_files(filename):
    filepaths = {
        os.path.join(UPLOAD_FOLDER, filename),
    }

    if filename.startswith('proc_'):
        filepaths.add(os.path.join(UPLOAD_FOLDER, filename.removeprefix('proc_')))
    else:
        filepaths.add(os.path.join(UPLOAD_FOLDER, f'proc_{filename}'))

    for filepath in filepaths:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                log(f'Deleted file {filepath}')
        except OSError as exc:
            log(f'Failed to delete file {filepath}: {exc}')


def get_uploads():
    if not db_ready:
        return []

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT filename, description, ocr_text FROM uploads ORDER BY id DESC")
        uploads = [
            {
                "filename": row[0],
                "description": row[1],
                "ocr_text": row[2],
            }
            for row in cur.fetchall()
        ]
        cur.close()
        conn.close()
        return uploads
    except OperationalError as exc:
        log(f'Failed to load uploads: {exc}')
        return []


def get_subscribers():
    if not db_ready:
        return []
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT email FROM subscribers ORDER BY created_at ASC")
        subs = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return subs
    except OperationalError as exc:
        log(f'Failed to load subscribers: {exc}')
        return []


def add_subscriber(email):
    if not db_ready:
        return False
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("INSERT INTO subscribers (email) VALUES (%s) ON CONFLICT DO NOTHING", (email,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except OperationalError as exc:
        log(f'Failed to add subscriber: {exc}')
        return False


def delete_subscriber(email):
    if not db_ready:
        return False
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("DELETE FROM subscribers WHERE email = %s", (email,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except OperationalError as exc:
        log(f'Failed to delete subscriber: {exc}')
        return False


def send_email_via_smtp(recipient, subject, body):
    import smtplib
    from email.message import EmailMessage

    smtp_host = os.getenv('SMTP_HOST')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER')
    smtp_pass = os.getenv('SMTP_PASS')
    email_from = os.getenv('EMAIL_FROM', smtp_user or 'ocr@example.com')
    use_ssl = os.getenv('SMTP_USE_SSL', 'false').lower() == 'true'
    use_starttls = os.getenv('SMTP_USE_STARTTLS', 'true').lower() == 'true'

    if not smtp_host:
        log('SMTP_HOST not configured; skipping email')
        return False

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = email_from
    msg['To'] = recipient
    msg.set_content(body)

    try:
        smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_cls(smtp_host, smtp_port) as server:
            if use_starttls and not use_ssl:
                server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        log(f'Email sent to {recipient}')
        return True
    except Exception as exc:
        log(f'Failed to send email to {recipient}: {exc}')
        return False

@app.route('/')
def index():
    return render_template('index.html', uploads=get_uploads(), subscribers=get_subscribers())


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/delete/<path:filename>', methods=['POST'])
def delete_upload(filename):
    delete_upload_files(filename)
    delete_upload_record(filename)
    log(f'Deleted upload {filename}')
    return redirect('/')

@app.route('/upload', methods=['POST'])
def upload():
    file = request.files['image']
    desc = request.form['description']
    
    if file:
        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(filepath)
        log(f'Received upload {file.filename}; publishing OCR task')
        # Publish a task to RabbitMQ for asynchronous processing
        msg = {
            'filename': file.filename,
            'filepath': filepath,
            'description': desc,
            'upload_folder': UPLOAD_FOLDER
        }
        try:
            rabbit_host = os.getenv('RABBITMQ_HOST', 'rabbitmq')
            rabbit_user = os.getenv('RABBITMQ_USER')
            rabbit_pass = os.getenv('RABBITMQ_PASS')
            creds = None
            if rabbit_user and rabbit_pass:
                creds = pika.PlainCredentials(rabbit_user, rabbit_pass)
            params = pika.ConnectionParameters(host=rabbit_host, credentials=creds) if creds else pika.ConnectionParameters(host=rabbit_host)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue='ocr_tasks', durable=True)
            channel.basic_publish(
                exchange='',
                routing_key='ocr_tasks',
                body=json.dumps(msg),
                properties=pika.BasicProperties(delivery_mode=2)
            )
            connection.close()
            log(f'Published OCR task for {file.filename}')
        except Exception as exc:
            log(f'Failed to publish message to RabbitMQ: {exc}')

    return redirect('/')


@app.route('/subscribe', methods=['POST'])
def subscribe():
    email = request.form.get('email')
    if not email:
        return redirect('/')

    added = add_subscriber(email)
    if added:
        # enqueue notifications for the worker to send (avoid requiring SMTP in frontend)
        uploads = get_uploads()
        try:
            rabbit_host = os.getenv('RABBITMQ_HOST', 'rabbitmq')
            rabbit_user = os.getenv('RABBITMQ_USER')
            rabbit_pass = os.getenv('RABBITMQ_PASS')
            creds = None
            if rabbit_user and rabbit_pass:
                creds = pika.PlainCredentials(rabbit_user, rabbit_pass)
            params = pika.ConnectionParameters(host=rabbit_host, credentials=creds) if creds else pika.ConnectionParameters(host=rabbit_host)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue='ocr_tasks', durable=True)
            for up in uploads:
                fname = up['filename']
                desc = up.get('description', '')
                ocr_text = up.get('ocr_text', '')
                msg = {
                    'filename': fname,
                    'description': desc,
                    'ocr_text': ocr_text,
                    'recipient': email,
                }
                channel.basic_publish(exchange='', routing_key='ocr_tasks', body=json.dumps(msg), properties=pika.BasicProperties(delivery_mode=2))
            connection.close()
            log(f'Enqueued {len(uploads)} catch-up notifications for {email}')
        except Exception as exc:
            log(f'Failed to enqueue catch-up notifications: {exc}')

    return redirect('/')


@app.route('/unsubscribe', methods=['POST'])
def unsubscribe():
    email = request.form.get('email')
    if email:
        deleted = delete_subscriber(email)
        if deleted:
            log(f'Removed subscriber {email}')
    return redirect('/')

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)