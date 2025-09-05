import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.linear_model import LinearRegression

print("✅ All libraries imported successfully!")

df = pd.DataFrame({"x": np.arange(10), "y": np.arange(10) * 2})
sns.scatterplot(data=df, x="x", y="y")
plt.show()