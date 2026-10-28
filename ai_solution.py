To solve this task, we need to determine the validity of each component in the lexicographic reward for the ParkAgent PRs by calculating the AUROC for each component.

### Approach
The solution involves the following steps:
1. **Load the necessary data**: We will load the data containing the PRs, their component scores, and the outcome (merged or rejected).
2. **Compute the AUROC for each component**: We will calculate the AUROC for each component (gate, quality, cost) to determine their validity.
3. **Determine validity based on AUROC**: We will check if each component's AUROC meets the threshold, which is likely 0.7 or higher.

### Solution Code
```python
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.metrics import auc
from sklearn.metrics import roc_curve

def compute_auroc(y_true, y_scores):
    fpr, tpr, thresholds = roc_curve(y_true, y_scores)
    auroc = auc(fpr, tpr)
    return auroc

# Load the data
data = pd.read_csv('path_to_data.csv')

# Split into components
components = ['gate', 'quality', 'cost']

valid_components = []

for component in components:
    # Extract the scores and true labels
    y_scores = data[component].values
    y_true = (data['outcome'] == 'merged').values  # outcome is 'merged' or 'rejected'

    # Compute AUROC
    auroc = compute_auroc(y_true, y_scores)
    valid_components.append((component, auroc))

# Determine validity based on AUROC threshold (e.g., 0.7)
threshold = 0.7
valid = [comp for comp, score in valid_components if score >= threshold]

print(f"Valid components: {valid}")
```

### Explanation
The code provided loads the data, computes the AUROC for each component, and checks if each component's AUROC meets the threshold. The valid components are then printed. This approach ensures that each component is evaluated based on its AUROC value to determine its validity in predicting the human outcome.