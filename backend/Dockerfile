FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache optimization)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The browser the crawl-first generate flow runs on. `pip install playwright`
# ships the client library only — the binary is a separate download, and without
# it every /api/analyze-crawl fails with 503 "a headless browser isn't available
# here" while the image looks correctly built. --with-deps pulls the system
# libraries Chromium needs on slim, which are not in the base image.
#
# Chromium only: the crawl uses one browser, and installing all three would add
# roughly half a gigabyte to every deploy for engines nothing launches.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8000

# --forwarded-allow-ips is what makes ratelimit.py work behind Caddy. Without it
# uvicorn only trusts X-Forwarded-For from 127.0.0.1, so every request arrives
# wearing the proxy's container IP and the per-IP limiters collapse into one
# bucket shared by the whole site — six signup emails an hour for everybody, and
# any anonymous /api/scan able to lock the world out of /api/auth/login.
#
# Private ranges only, never "*": uvicorn trusts the *peer* before reading the
# header, then takes the right-most untrusted hop — which is the address Caddy
# appended, and a client cannot forge. "*" would instead take the left-most hop,
# which is whatever the client typed, handing anyone an unlimited mailer.
# Exposed with no proxy, the peer is public, fails the trust check, and the
# header is ignored — so this stays correct off the compose stack too.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"]
