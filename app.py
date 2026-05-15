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

@app.route('/')
def index():
    return render_template('index.html', uploads=get_uploads())


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

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

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)