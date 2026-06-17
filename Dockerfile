FROM python:3.11-slim

WORKDIR /app
COPY app /app

RUN pip install flask requests python-dotenv vobject cryptography

ENV PROFILE_STORE_PATH=/data/profiles.db
ENV DEFAULT_RADICALE_URL=https://radicale.example.test
EXPOSE 5000

CMD ["python", "main.py"]
