import os, psycopg2, pytesseract
from flask import Flask, render_template, request, redirect
from PIL import Image, ImageDraw

app = Flask(__name__)
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
db_pass = os.getenv('DB_PASS')
db_url = f"host=ocr-db dbname=ocrdb user=user password={db_pass}"

# Adatbázis inicializálás [cite: 92]
def init_db():
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS uploads 
                   (id SERIAL PRIMARY KEY, filename TEXT, description TEXT, ocr_text TEXT)''')
    conn.commit()
    cur.close()
    conn.close()

@app.route('/')
def index():
    return render_template('index.html')

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
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("INSERT INTO uploads (filename, description, ocr_text) VALUES (%s, %s, %s)",
                    (proc_filename, desc, " ".join(full_text)))
        conn.commit()
        cur.close()
        conn.close()
        
    return redirect('/')

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)