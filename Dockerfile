# Use an official Python runtime as a parent image
FROM python:3.12.0-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN --mount=type=cache,id=pip-cache,target=/root/.cache/pip \
    python -m venv /opt/venv && \
    . /opt/venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# Make sure the PATH includes the virtualenv binaries
ENV PATH="/opt/venv/bin:$PATH"

# Run the bot
CMD ["python", "bot.py"]
