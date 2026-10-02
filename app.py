import os
import sqlite3
from flask import Flask, render_template_string

app = Flask(__name__)

@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>GrowthCRM - Live</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-950 text-white flex items-center justify-center min-h-screen">
        <div class="text-center p-8 max-w-xl">
            <h1 class="text-4xl font-extrabold text-emerald-400 mb-4">GrowthCRM is Live! 🚀</h1>
            <p class="text-slate-400 mb-6">Your backend server is successfully deployed and running on Render.</p>
            <a href="https://selar.com/9u69r59d57" target="_blank" class="bg-emerald-500 text-slate-950 font-bold px-6 py-3 rounded-xl">Purchase Subscription via Selar</a>
        </div>
    </body>
    </html>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
