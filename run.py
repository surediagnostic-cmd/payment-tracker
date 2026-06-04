import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from app import create_app
app = create_app()
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(port=port, host="0.0.0.0")
