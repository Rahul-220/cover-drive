# Lambda Python base image
FROM public.ecr.aws/lambda/python:3.11

# Copy requirements and install (keeps image small)
COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy your app code (backend + scripts folder)
COPY backend ./backend
COPY scripts ./scripts

# (Optional) If you want to bake parquet files into the image:
# COPY data/parquet ./data/parquet

# Tell Lambda which handler to run
# (this matches: handler = Mangum(app) in backend/app.py)
CMD ["backend.app.handler"]
