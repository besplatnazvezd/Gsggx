import aiosqlite

async def init_db():
    async with aiosqlite.connect("database.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance_mcoin INTEGER DEFAULT 0,
                total_deposited INTEGER DEFAULT 0,
                total_gmp_withdrawn REAL DEFAULT 0,
                username TEXT
            )
        """)
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect("database.db") as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def create_user(user_id, username):
    async with aiosqlite.connect("database.db") as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        await db.commit()

async def update_balance(user_id, amount):
    async with aiosqlite.connect("database.db") as db:
        await db.execute("UPDATE users SET balance_mcoin = balance_mcoin + ?, total_deposited = total_deposited + ? WHERE user_id = ?", 
                         (amount, amount, user_id))
        await db.commit()

async def subtract_balance(user_id, mcoin_amount, gmp_amount):
    async with aiosqlite.connect("database.db") as db:
        await db.execute("UPDATE users SET balance_mcoin = balance_mcoin - ?, total_gmp_withdrawn = total_gmp_withdrawn + ? WHERE user_id = ?", 
                         (mcoin_amount, gmp_amount, user_id))
        await db.commit()
