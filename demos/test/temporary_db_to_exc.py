import sqlite3
import pandas as pd

# Database file
db_path = "cegtar_masolat.db"

# Connect to database
conn = sqlite3.connect(db_path)

# Get all table names
query = "SELECT name FROM sqlite_master WHERE type='table';"
tables = pd.read_sql(query, conn)

# Create Excel writer
output_file = "output.xlsx"
with pd.ExcelWriter(output_file, engine="xlsxwriter") as writer:
    for table_name in tables['name']:
        # Read each table
        df = pd.read_sql(f"SELECT * FROM {table_name}", conn)
        
        # Write to Excel (each table = separate sheet)
        df.to_excel(writer, sheet_name=table_name[:31], index=False)

# Close connection
conn.close()

print("Done. Excel file created:", output_file)