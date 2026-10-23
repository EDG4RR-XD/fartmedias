FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py economy.py items.json sets.json draw_pool.json modifiers.json admin_gradient.png cloudy.png blood_moon.png ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
