FROM python

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rotation_queue.py .

CMD [ "python", "./rotation_queue.py" ]