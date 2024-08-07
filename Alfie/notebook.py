# %% Import and manage libraries and packages
import torch
from transformer_lens import HookedTransformer
import pandas as pd
import numpy as np
import tqdm
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split
import einops
from ivy import to_numpy
import plotly_express as px

# Enable gradient calculations (by default)

torch.set_grad_enabled(True)

# %% Load GPT-2 small model from transformer_lens

model = HookedTransformer.from_pretrained("gpt2-small")

# %% Initialise model components as variables

n_layers = model.cfg.n_layers  # Number of transformer layers
d_model = model.cfg.d_model    # Dimension of the model
n_heads = model.cfg.n_heads    # Number of attention heads
d_head = model.cfg.d_head      # Dimension of each attention head
d_mlp = model.cfg.d_mlp        # Dimension of the MLP (Feed Forward network) within the transformer
d_vocab = model.cfg.d_vocab    # Size of the vocabulary

# %% Load and display common words

common_words = open("../common_words.txt", "r").read().split("\n")
print(common_words[:10])

# %% Calculate the number of tokens for each common word

num_tokens = [len(model.to_tokens(" " + word, prepend_bos=False).squeeze(0)) for word in common_words]
print(list(zip(num_tokens, common_words))[:10])

# %% Create a DataFrame of words and their token counts
word_df = pd.DataFrame({"word": common_words, "num_tokens": num_tokens})
word_df = word_df.query('num_tokens < 4')  # Filter words with less than 4 tokens
word_df.value_counts("num_tokens")

# %% Define the prefix for context and set parameters

prefix = "The United States Declaration of Independence received its first formal public reading, in Philadelphia.\nWhen"
PREFIX_LENGTH = len(model.to_tokens(prefix, prepend_bos=True).squeeze(0))
NUM_WORDS = 7
MAX_WORD_LENGTH = 3

# %% Split the data into training and testing sets

train_filter = np.random.rand(len(word_df)) < 0.8
train_word_df = word_df.iloc[train_filter]
test_word_df = word_df.iloc[~train_filter]
print(train_word_df.value_counts("num_tokens"))
print(test_word_df.value_counts("num_tokens"))

# Group words by their token length

train_word_by_length_array = [np.array([" " + j for j in train_word_df[train_word_df.num_tokens == i].word.values]) for i in range(1, MAX_WORD_LENGTH + 1)]
test_word_by_length_array = [np.array([" " + j for j in test_word_df[test_word_df.num_tokens == i].word.values]) for i in range(1, MAX_WORD_LENGTH + 1)]

# %% Define a function to generate batches of tokenized inputs

def gen_batch(batch_size, word_by_length_array):
    word_lengths = torch.randint(1, MAX_WORD_LENGTH+1, (batch_size, NUM_WORDS))
    words = []
    for i in range(batch_size):
        row = []
        for word_len in word_lengths[i].tolist():
            word = word_by_length_array[word_len-1][np.random.randint(len(word_by_length_array[word_len-1]))]
            row.append(word)
        words.append("".join(row))
    full_tokens = torch.ones((batch_size, PREFIX_LENGTH + MAX_WORD_LENGTH*NUM_WORDS), dtype=torch.int64)
    tokens = model.to_tokens([prefix + word for word in words], prepend_bos=True)
    full_tokens[:, :tokens.shape[-1]] = tokens
    
    first_token_indices = torch.concatenate([
        torch.zeros(batch_size, dtype=int)[:, None], word_lengths.cumsum(dim=-1)[..., :-1]
    ], dim=-1) + PREFIX_LENGTH
    
    last_token_indices = word_lengths.cumsum(dim=-1) - 1 + PREFIX_LENGTH
    return full_tokens, words, word_lengths, first_token_indices, last_token_indices

# Generate a batch of tokens and their related information

tokens, words, word_lengths, first_token_indices, last_token_indices = gen_batch(10, train_word_by_length_array)
tokens, words, word_lengths, first_token_indices, last_token_indices

# %% Set training parameters

batch_size = 256
epochs = 1000

# %% Collect residuals for tokens across multiple epochs

torch.set_grad_enabled(False)
epochs = 100
all_first_token_residuals = []
all_last_token_residuals = []
                                                        
for i in tqdm.tqdm(range(epochs)):
    tokens, words, word_lengths, first_token_indices, last_token_indices = gen_batch(batch_size, train_word_by_length_array)
    with torch.no_grad():
        # _, cache = model.run_with_cache(tokens.cuda(), names_filter=lambda x: x.endswith("resid_post"))
        _, cache = model.run_with_cache(tokens, names_filter=lambda x: x.endswith("resid_post")) # Can't run run_with_cache with CUDA on my Mac, just passing 'tokens' instead 
        residuals = cache.stack_activation("resid_post")
        first_token_residuals = residuals[:, torch.arange(len(first_token_indices)).to(residuals.device)[:, None], first_token_indices, :]
        last_token_residuals = residuals[:, torch.arange(len(last_token_indices)).to(residuals.device)[:, None], last_token_indices, :]
        print("Shapes", first_token_residuals.shape, last_token_residuals.shape)
        all_first_token_residuals.append(to_numpy(first_token_residuals))
        all_last_token_residuals.append(to_numpy(last_token_residuals))

"""
Run from here to avoid repeating 100 epoch runs!
"""

# %% Data preparation for training a Logistic Regression model

LAYER = 0
y = np.array([j for i in range(len(all_first_token_residuals[0])) for j in range(NUM_WORDS)])
layer_data = all_last_token_residuals[LAYER]
X = layer_data[:, :].reshape(-1, d_model)

# Split the dataset into train and test sets

x_indices = to_numpy(torch.randperm(len(X))[:10000])
y_indices = to_numpy(torch.randperm(len(y))[:10000])
common_indices = np.intersect1d(x_indices, y_indices)

X_train, X_test, y_train, y_test = train_test_split(X[common_indices], y[common_indices], test_size=0.1)

# Create and train a Logistic Regression model

lr_model = LogisticRegression(multi_class='ovr', solver='saga', random_state=42, max_iter=100, C=1.0)
lr_model.fit(X_train, y_train)

# Evaluate the model

y_pred = lr_model.predict(X_train)
print(classification_report(y_train, y_pred))
y_pred = lr_model.predict(X_test)
print(classification_report(y_test, y_pred))

# %% Generate predictions on test batches

test_batches = 10
last_token_predictions_list = []
last_token_abs_indices_list = []

with torch.no_grad():
    for i in tqdm.tqdm(range(test_batches)):
        tokens, words, word_lengths, first_token_indices, last_token_indices = gen_batch(batch_size, test_word_by_length_array)
        _, cache = model.run_with_cache(tokens, names_filter=lambda x: x.endswith("resid_post"))
        residuals = cache.stack_activation("resid_post")
        first_token_residuals = residuals[:, torch.arange(len(first_token_indices)).to(residuals.device)[:, None], first_token_indices, :]
        last_token_residuals = residuals[:, torch.arange(len(last_token_indices)).to(residuals.device)[:, None], last_token_indices, :]
        last_token_resids = to_numpy(einops.rearrange(last_token_residuals[LAYER], "batch word d_model -> (batch word) d_model"))
        last_token_predictions_list.append(lr_model.predict(last_token_resids))
        last_token_abs_indices_list.append(to_numpy(last_token_indices.flatten()))

# %% Prepare and visualize results

last_token_abs_indices = np.concatenate(last_token_abs_indices_list)
last_token_predictions = np.concatenate(last_token_predictions_list)

df = pd.DataFrame({
    "index": [i for _ in range(batch_size * test_batches) for i in range(NUM_WORDS)],
    "abs_pos": last_token_abs_indices,
    "pred": last_token_predictions
})

# %% Print dataframe
print(df)

#%% Plot histogram of the prediction results

# px.histogram(df, x="abs_pos", color="pred", facet_row="index", barnorm="percent").show()
px.histogram(df, x="abs_pos", color="pred", facet_row="index").show()

# %%
# git test commit - Paul