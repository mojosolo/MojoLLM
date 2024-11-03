import argparse
import torch
from torch.utils.data import Dataset, DataLoader, SubsetRandomSampler
from transformers import AutoTokenizer, AutoModel
from transformers import PreTrainedModel, PretrainedConfig, AutoConfig
from datasets import load_dataset
from sklearn.model_selection import KFold
from tqdm import tqdm
import torch.nn.functional as F
import torch.nn as nn
from safetensors.torch import save_model, load_model
from collections import Counter
from torch.optim.lr_scheduler import ReduceLROnPlateau
import numpy as np

# Constants
APPROACHES = ["none", "mcts", "bon", "moa", "rto", "z3", "self_consistency", "pvg", "rstar", "cot_reflection", "plansearch", "leap", "re2"]
MAX_LENGTH = 512

# Device selection
device = torch.device("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
print(f"Using device: {device}")

class OptILMDataset(Dataset):
    def __init__(self, prompts, approaches, ranks, tokens, tokenizer):
        self.prompts = prompts
        self.approaches = approaches
        self.ranks = ranks
        self.tokens = tokens
        self.tokenizer = tokenizer

    def __len__(self):
        return len(self.prompts)

    def __getitem__(self, idx):
        prompt = self.prompts[idx]
        approaches = self.approaches[idx]
        ranks = self.ranks[idx]
        tokens = self.tokens[idx]

        encoding = self.tokenizer.encode_plus(
            prompt,
            add_special_tokens=True,
            max_length=MAX_LENGTH,
            padding='max_length',
            truncation=True,
            return_attention_mask=True,
            return_tensors='pt'
        )

        return {
            'input_ids': encoding['input_ids'].flatten(),
            'attention_mask': encoding['attention_mask'].flatten(),
            'approaches': torch.tensor([APPROACHES.index(approach) for approach in approaches], dtype=torch.long),
            'ranks': torch.tensor(ranks, dtype=torch.float),
            'tokens': torch.tensor(tokens, dtype=torch.float),
        }

def load_and_preprocess_data(tokenizer):
    dataset = load_dataset('json', data_files='optillm_dataset.jsonl')
    
    data_items = []

    for item in dataset['train']:
        prompt = item['prompt']
        results = item['results']
        
        if not results:
            continue
        
        # Filter results to only include approaches with valid ranks and tokens
        valid_results = [result for result in results if result['rank'] is not None and 'tokens' in result]
        
        # Check if we have all 13 approaches
        if len(valid_results) != 13:
            continue
        
        # Sort the results by approach to ensure consistent ordering
        valid_results.sort(key=lambda x: APPROACHES.index(x['approach']))
        
        approaches = [result['approach'] for result in valid_results]
        ranks = [result['rank'] for result in valid_results]
        tokens = [result['tokens'] for result in valid_results]

        data_items.append({
            'prompt': prompt,
            'approaches': approaches,
            'ranks': ranks,
            'tokens': tokens
        })

    print(f"Total data points: {len(data_items)}")
    print(f"Unique prompts: {len(set(item['prompt'] for item in data_items))}")
    approach_counts = Counter(approach for item in data_items for approach in item['approaches'])
    print("Approach distribution:")
    for approach, count in approach_counts.items():
        print(f"  {approach}: {count}")

    return OptILMDataset(
        [item['prompt'] for item in data_items],
        [item['approaches'] for item in data_items],
        [item['ranks'] for item in data_items],
        [item['tokens'] for item in data_items],
        tokenizer
    )

def calculate_accuracy(predictions, labels):
    return (predictions == labels).float().mean()

class OptILMClassifier(nn.Module):
    def __init__(self, base_model, num_labels):
        super().__init__()
        self.base_model = base_model
        self.effort_encoder = nn.Sequential(
            nn.Linear(1, 64),
            nn.ReLU(),
            nn.Linear(64, 64),
            nn.ReLU()
        )
        self.classifier = nn.Linear(base_model.config.hidden_size + 64, num_labels)

    def forward(self, input_ids, attention_mask, effort):
        outputs = self.base_model(input_ids=input_ids, attention_mask=attention_mask)
        pooled_output = outputs.last_hidden_state[:, 0]  # Shape: (batch_size, hidden_size)
        effort_encoded = self.effort_encoder(effort.unsqueeze(1))  # Shape: (batch_size, 64)
        combined_input = torch.cat((pooled_output, effort_encoded), dim=1)
        logits = self.classifier(combined_input)
        return logits

def train(model, train_dataloader, val_dataloader, optimizer, scheduler, num_epochs, patience, clip_value):
    best_val_accuracy = 0.0
    epochs_without_improvement = 0
    
    for epoch in range(num_epochs):
        model.train()
        total_loss = 0
        total_accuracy = 0
        
        for batch in tqdm(train_dataloader, desc=f"Epoch {epoch+1}/{num_epochs}"):
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            approaches = batch['approaches'].to(device)
            ranks = batch['ranks'].to(device)
            tokens = batch['tokens'].to(device)

            # Normalize tokens to [0, 1] range as a proxy for effort
            effort = (tokens - tokens.min()) / (tokens.max() - tokens.min())

            # Use the minimum rank (best approach) for each prompt
            best_approach_indices = ranks.argmin(dim=1)
            
            logits = model(input_ids, attention_mask, effort[:, 0])  # Use effort for the best approach
            
            # Calculate standard cross-entropy loss
            ce_loss = F.cross_entropy(logits, best_approach_indices)
            
            # Calculate effort-sensitive loss
            effort_loss = F.mse_loss(logits.softmax(dim=1).gather(1, best_approach_indices.unsqueeze(1)).squeeze(), effort[:, 0])
            
            # Combine losses
            loss = ce_loss + 0.1 * effort_loss  # Adjust the weight of effort_loss as needed

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), clip_value)
            optimizer.step()
            optimizer.zero_grad()

            total_loss += loss.item()
            predictions = torch.argmax(logits, dim=-1)
            total_accuracy += calculate_accuracy(predictions, best_approach_indices)

        avg_train_loss = total_loss / len(train_dataloader)
        avg_train_accuracy = total_accuracy / len(train_dataloader)
        
        # Validation
        avg_val_accuracy = validate(model, val_dataloader)
        
        print(f"Epoch {epoch+1}/{num_epochs}, Train Loss: {avg_train_loss:.4f}, Train Accuracy: {avg_train_accuracy:.4f}, Val Accuracy: {avg_val_accuracy:.4f}")
        
        # Learning rate scheduling
        if isinstance(scheduler, ReduceLROnPlateau):
            scheduler.step(avg_val_accuracy)
        else:
            scheduler.step()
        
        if avg_val_accuracy > best_val_accuracy:
            best_val_accuracy = avg_val_accuracy
            epochs_without_improvement = 0
            save_model(model, "best_model.safetensors")
        else:
            epochs_without_improvement += 1
            
        if epochs_without_improvement >= patience:
            print(f"Early stopping triggered after {epoch+1} epochs")
            break

def validate(model, val_dataloader):
    model.eval()
    total_val_accuracy = 0
    
    with torch.no_grad():
        for batch in val_dataloader:
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            approaches = batch['approaches'].to(device)
            ranks = batch['ranks'].to(device)
            tokens = batch['tokens'].to(device)

            effort = (tokens - tokens.min()) / (tokens.max() - tokens.min())
            best_approach_indices = ranks.argmin(dim=1)

            logits = model(input_ids, attention_mask, effort[:, 0])
            predictions = torch.argmax(logits, dim=-1)
            total_val_accuracy += calculate_accuracy(predictions, best_approach_indices)

    return total_val_accuracy / len(val_dataloader)

def inference(model, tokenizer, prompt, effort_levels):
    model.eval()
    with torch.no_grad():
        inputs = tokenizer(prompt, return_tensors="pt", max_length=MAX_LENGTH, truncation=True, padding="max_length")
        input_ids = inputs["input_ids"].to(device)
        attention_mask = inputs["attention_mask"].to(device)
        
        results = []
        for effort in effort_levels:
            effort_tensor = torch.tensor([effort], dtype=torch.float).to(device)
            logits = model(input_ids, attention_mask, effort_tensor)
            probabilities = F.softmax(logits, dim=1)
            predicted_approach_index = torch.argmax(probabilities, dim=1).item()
            results.append((APPROACHES[predicted_approach_index], probabilities[0][predicted_approach_index].item()))
        
    return results

def main(args):
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    dataset = load_and_preprocess_data(tokenizer)

    kf = KFold(n_splits=args.k_folds, shuffle=True, random_state=42)
    
    best_val_accuracy = 0
    best_fold = 0
    
    for fold, (train_indices, val_indices) in enumerate(kf.split(dataset), 1):
        print(f"\nTraining Fold {fold}")
        
        train_sampler = SubsetRandomSampler(train_indices)
        val_sampler = SubsetRandomSampler(val_indices)
        
        train_dataloader = DataLoader(dataset, batch_size=args.batch_size, sampler=train_sampler)
        val_dataloader = DataLoader(dataset, batch_size=args.batch_size, sampler=val_sampler)

        base_model = AutoModel.from_pretrained(args.model_name)
        model = OptILMClassifier(base_model, num_labels=len(APPROACHES)).to(device)

        optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=0.01)
        scheduler = ReduceLROnPlateau(optimizer, mode='max', factor=0.1, patience=2, verbose=True)

        train(model, train_dataloader, val_dataloader, optimizer, scheduler, args.num_epochs, args.patience, args.clip_value)

        # Evaluate the model on the validation set
        fold_val_accuracy = validate(model, val_dataloader)
        print(f"Fold {fold} Validation Accuracy: {fold_val_accuracy:.4f}")

        # Save the model for this fold
        save_model(model, f"model_fold_{fold}.safetensors")

        # Update best model if this fold performed better
        if fold_val_accuracy > best_val_accuracy:
            best_val_accuracy = fold_val_accuracy
            best_fold = fold
            save_model(model, "best_model.safetensors")

    print(f"\nBest performing model was from fold {best_fold} with validation accuracy {best_val_accuracy:.4f}")

    if args.push_to_hub:
        base_model = AutoModel.from_pretrained(args.model_name)
        # best_model = OptILMClassifier(base_model, num_labels=len(APPROACHES))
        # best_model.to(device)
        # load_model(best_model, "best_model.safetensors")
        # we just push the base model and then upload the safetensors file manually as OptILMClassifier class doesn't have a push_to_hub method.
        base_model.push_to_hub(args.hub_model_id)
        tokenizer.push_to_hub(args.hub_model_id)

    # Load the best model for inference
    base_model = AutoModel.from_pretrained(args.model_name)
    best_model = OptILMClassifier(base_model, num_labels=len(APPROACHES))
    best_model.to(device)
    load_model(best_model, "best_model.safetensors")
    best_model.eval()

    test_prompts = [
        "Maximize x + y subject to: x + 2y <= 10, x >= 0, y >= 0",
        "Find the shortest path between nodes A and B in the given graph",
        "Solve the Tower of Hanoi problem with 4 disks",
        "Determine if the given number is prime",
        "Find all possible combinations of coins that sum up to $1"
    ]

    effort_levels = [0.0, 0.2, 0.5, 0.8, 1.0]

    print("\nInference Examples:")
    for prompt in test_prompts:
        print(f"\nTest Prompt: {prompt}")
        results = inference(best_model, tokenizer, prompt, effort_levels)
        for effort, (approach, confidence) in zip(effort_levels, results):
            print(f"Effort: {effort:.1f}, Predicted Approach: {approach}, Confidence: {confidence:.4f}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train OptILM classifier")
    parser.add_argument("--model_name", type=str, default="google-bert/bert-large-uncased", help="Pretrained model name")
    parser.add_argument("--batch_size", type=int, default=4, help="Batch size for training")
    parser.add_argument("--learning_rate", type=float, default=1e-6, help="Learning rate")
    parser.add_argument("--num_epochs", type=int, default=10, help="Maximum number of training epochs")
    parser.add_argument("--push_to_hub", action="store_true", help="Push model to Hugging Face Hub")
    parser.add_argument("--hub_model_id", type=str, help="Model ID for Hugging Face Hub")
    parser.add_argument("--k_folds", type=int, default=5, help="Number of folds for cross-validation")
    parser.add_argument("--patience", type=int, default=3, help="Number of epochs to wait for improvement before early stopping")
    parser.add_argument("--clip_value", type=float, default=1.0, help="Gradient clipping value")
    
    args = parser.parse_args()
    main(args)