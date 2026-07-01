python3 -c "
import pandas as pd
df = pd.read_excel('/home/umar/Desktop/cctv2/sheet/complete_results_20260313_160604.xlsx')
print('Columns:', list(df.columns))
print()
print('First 2 rows:')
print(df.head(2))
"