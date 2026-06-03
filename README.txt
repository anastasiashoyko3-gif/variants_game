Варіанти V4 stable

Локально:
pip install -r requirements.txt
python app.py

Render:
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app

Environment Variables:
ADMIN_PASSWORD=твій пароль адмінки
GAME_PASSWORD=пароль для гравців
SECRET_KEY=довгий секретний текст

Пізніше для постійного зберігання бази й фото краще підключити Supabase/Cloudinary.
