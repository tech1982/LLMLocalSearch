#!/bin/bash
set -e

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════╗"
echo "║   🔍 TG & Insta Semantic Search — Setup          ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"

# 1. Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker not found. Install Docker Desktop for Mac.${NC}"
    echo "   https://docs.docker.com/desktop/install/mac-install/"
    exit 1
fi

if ! docker info &> /dev/null 2>&1; then
    echo -e "${RED}❌ Docker daemon is not running. Open Docker Desktop.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Docker found${NC}"

# 2. Create .env if not exists
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${YELLOW}📝 Created .env from the template. You must edit it before continuing!${NC}"
    echo ""
    echo -e "${YELLOW}   Open .env and fill in:${NC}"
    echo "   - TG_API_ID and TG_API_HASH (from https://my.telegram.org/apps)"
    echo "   - TG_PHONE (your phone number)"
    echo "   - TG_CHANNELS (channels or groups to index)"
    echo "   - INSTA_USERNAME and INSTA_PASSWORD (optional)"
    echo ""
    echo -e "${YELLOW}   After editing, run this script again.${NC}"
    exit 0
fi

# 3. Validate .env
source .env
if [ "$TG_API_ID" = "12345678" ] || [ -z "$TG_API_ID" ]; then
    echo -e "${RED}❌ Fill in TG_API_ID in .env${NC}"
    exit 1
fi

echo -e "${GREEN}✅ .env is configured${NC}"

# 4. Create directories
mkdir -p data sessions ollama_models
echo -e "${GREEN}✅ Directories created${NC}"

# 5. Build and start
echo -e "\n${CYAN}🐳 Building Docker containers...${NC}"
docker compose build

echo -e "\n${CYAN}🚀 Starting services...${NC}"
docker compose up -d

# 6. Wait for Ollama and pull model
echo -e "\n${CYAN}⏳ Waiting for Ollama...${NC}"
sleep 5
for i in {1..30}; do
    if docker exec ollama-search ollama list &> /dev/null 2>&1; then
        break
    fi
    sleep 2
done

OLLAMA_MODEL=${OLLAMA_MODEL:-"gemma3:4b"}
echo -e "${CYAN}📥 Downloading model ${OLLAMA_MODEL}...${NC}"
docker exec ollama-search ollama pull "$OLLAMA_MODEL" || {
    echo -e "${YELLOW}⚠️ Could not download the model. Try again later:${NC}"
    echo "   docker exec ollama-search ollama pull $OLLAMA_MODEL"
}

# 7. Done
echo -e "\n${GREEN}"
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✅ Everything is ready!                         ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║                                                  ║"
echo "║   🌐 Web UI:  http://localhost:8501               ║"
echo "║                                                  ║"
echo "║   📱 Telegram indexing:                           ║"
echo "║   docker exec -it semantic-search \               ║"
echo "║     python src/ingest_telegram.py                 ║"
echo "║                                                  ║"
echo "║   📸 Instagram indexing:                          ║"
echo "║   docker exec -it semantic-search \               ║"
echo "║     python src/ingest_instagram.py                ║"
echo "║                                                  ║"
echo "║   🛑 Stop:  docker compose down                   ║"
echo "║   ▶️  Start: docker compose up -d                  ║"
echo "║                                                  ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "${YELLOW}⚡ First step: run Telegram indexing.${NC}"
echo -e "${YELLOW}   On the first run, Telethon will ask for the Telegram confirmation code.${NC}"
