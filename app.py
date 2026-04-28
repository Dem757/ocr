import os, psycopg2, pytesseract
from psycopg2 import OperationalError
from flask import Flask, render_template, request, redirect, send_from_directory
from PIL import Image, ImageDraw

app = Flask(__name__)
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
db_pass = os.getenv('DB_PASS')
db_url = os.getenv('DATABASE_URL') or f"host=ocr-db port=5444 dbname=ocrdb user=user password={db_pass}"
db_ready = False

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
        print(f'Database unavailable during startup: {exc}')


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
        print(f'Failed to save upload record: {exc}')


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
        print(f'Failed to load uploads: {exc}')
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
        
        # OCR és Bekeretezés [cite: 93]
        img = Image.open(filepath)
        d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        draw = ImageDraw.Draw(img)
        
        full_text = []
        for i in range(len(d['text'])):
            if int(d['conf'][i]) > 60:
                (x, y, w, h) = (d['left'][i], d['top'][i], d['width'][i], d['height'][i])
                draw.rectangle([x, y, x + w, y + h], outline="red", width=3)
                full_text.append(d['text'][i])
        
        proc_filename = "proc_" + file.filename
        img.save(os.path.join(UPLOAD_FOLDER, proc_filename))
        
        # Mentés adatbázisba [cite: 92]
        save_upload_record(proc_filename, desc, " ".join(full_text))
        
    return redirect('/')

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)