from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/portfolio')
def portfolio():
    return render_template('portfolio.html')

@app.route('/moreworks')
def portfolio2():
    return render_template('portfolio2.html')

if __name__ == '__main__':
    app.run(debug=True)
