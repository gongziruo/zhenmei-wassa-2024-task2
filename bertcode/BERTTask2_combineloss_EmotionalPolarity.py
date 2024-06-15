import torch
from transformers import BertTokenizer, BertForSequenceClassification, AdamW
from torch.utils.data import DataLoader, Dataset
import pandas as pd
from tqdm import tqdm
import random
import torch.nn as nn
import torch.nn.functional as F
import os
import numpy as np
from torchmetrics import PearsonCorrCoef


def create_hierarchical_penalty_matrix(num_classes, thegma):
    """
    Create a matrix of size num_classes x num_classes where each entry (i, j)
    contains the hierarchical penalty for predicting class j when the true class is i.
    """
    # Generate a grid of label indices
    indices = torch.arange(num_classes).unsqueeze(0)
    # Calculate the absolute difference between indices and transpose
    absolute_differences = torch.abs(indices - indices.T)
    # Calculate the hierarchical penalty matrix
    penalty_matrix = torch.exp(-thegma * absolute_differences)
    return penalty_matrix


class CombinedLoss(nn.Module):
    def __init__(self, num_classes, alpha, beta, thegma):
        super(CombinedLoss, self).__init__()
        self.num_classes = num_classes
        self.alpha = alpha
        self.beta = beta  # 新增参数beta用于调节两个loss的权重
        self.thegma = thegma 
        # 预计算罚分矩阵
        self.penalty_matrix = create_hierarchical_penalty_matrix(num_classes, thegma)

    def forward(self, logits, targets):
        # 确保罚分矩阵与logits在同一个设备上
        self.penalty_matrix = self.penalty_matrix.to(logits.device)
        
        # 计算标准的交叉熵损失
        ce_loss = F.cross_entropy(logits, targets, reduction='none')
        
        # 根据真实类别收集每个预测对应的罚分
        penalties = self.penalty_matrix[targets, :]
        
        # 应用罚分到log概率上
        log_probs = F.log_softmax(logits, dim=1)
        weighted_log_probs = penalties * log_probs
        
        # 计算最终的加权log概率损失
        structured_contrastive_loss = -torch.sum(weighted_log_probs, dim=1).mean()

        # 计算Pearson相关损失
        logits_flat = logits.view(-1)
        targets_one_hot = F.one_hot(targets, num_classes=self.num_classes).float()
        targets_flat = targets_one_hot.view(-1)
        
        logits_mean = logits_flat.mean()
        targets_mean = targets_flat.mean()
        
        logits_centered = logits_flat - logits_mean
        targets_centered = targets_flat - targets_mean
        
        correlation = torch.sum(logits_centered * targets_centered) / (torch.sqrt(torch.sum(logits_centered ** 2)) * torch.sqrt(torch.sum(targets_centered ** 2)))
        pearson_loss = -correlation

        # 结合两种损失
        combined_loss = self.alpha * structured_contrastive_loss + self.beta * pearson_loss

        return combined_loss

df = pd.read_csv("/root/trac2_CONVT_train_p.csv",encoding='ISO-8859-1')
df_dev = pd.read_csv("/root/trac2_CONVT_dev.csv",encoding='ISO-8859-1')

# EmotionClass:
emotion_bins = [-0.25, 0.25, 0.75, 1.25, 1.75, 2.25, 2.75,3.5,4.5,5.5]
emotion_groups = [0, 1, 2, 3, 4, 5, 6, 7, 8]
# EmotionalPolarityClass:
emotionalPolarity_bins = [-0.25, 0.25, 0.75, 1.25, 1.75, 3]
emotionalPolarity_groups = [0, 1, 2, 3,4]

# EmpathyClass:
empathy_bins = [-0.25, 0.25, 0.75, 1.25, 1.75, 2.25, 2.75,3.25,3.75,4.25,4.75,5.5]
empathy_groups = [0, 1, 2,3,4,5, 6,7, 8,9, 10]


# turn discrete values into classes
def value_to_class(bins, groups, df_name, column_name):
    class_col = column_name + "Class"
    df_name[class_col] = pd.cut(df_name[column_name], bins, labels=groups)


# train, get class labels
value_to_class(emotion_bins, emotion_groups, df, 'Emotion')
value_to_class(emotionalPolarity_bins, emotionalPolarity_groups, df, 'EmotionalPolarity')
value_to_class(empathy_bins, empathy_groups, df, 'Empathy')
# dev, get class labels
value_to_class(emotion_bins, emotion_groups, df_dev, 'Emotion')
value_to_class(emotionalPolarity_bins, emotionalPolarity_groups, df_dev, 'EmotionalPolarity')
value_to_class(empathy_bins, empathy_groups, df_dev, 'Empathy')


# online loading:
# tokenizer = BertTokenizer.from_pretrained('bert-base-chinese')
# model = BertForSequenceClassification.from_pretrained('bert-base-chinese')

# local loading:
tokenizer = BertTokenizer.from_pretrained('/root/bert-base-uncased')
model = BertForSequenceClassification.from_pretrained('/root/bert-base-uncased', num_labels=len(emotionalPolarity_groups))

# random the order of samples
random.seed(42)
df = df.sample(frac=1).reset_index(drop=True)
df_dev = df_dev.sample(frac=1).reset_index(drop=True)


# # 数据集中1为正面，0为反面
class Task2Dataset(Dataset):
    def __init__(self, dataframe, tokenizer, max_length=128):
        self.dataframe = dataframe
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self):
        return len(self.dataframe)

    def __getitem__(self, idx):
        text = self.dataframe.iloc[idx]['text']
        label = self.dataframe.iloc[idx]['EmotionalPolarity']
        encoding = self.tokenizer(text, padding='max_length', truncation=True, max_length=self.max_length, return_tensors='pt')
        return {
            'input_ids': encoding['input_ids'].flatten(),
            'attention_mask': encoding['attention_mask'].flatten(),
            'labels': torch.tensor(label, dtype=torch.long)
        }


# build dataset with tokenizer
train_dataset = Task2Dataset(df[:], tokenizer)
dev_dataset = Task2Dataset(df_dev[:], tokenizer)

# data_loader
train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
dev_loader = DataLoader(dev_dataset, batch_size=32, shuffle=False)

# params
optimizer = AdamW(model.parameters(), lr=5e-6)
# 使用多个GPU
if torch.cuda.device_count() > 1:
    print(f"Let's use {torch.cuda.device_count()} GPUs!")
    model = nn.DataParallel(model)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model.to(device)
pearson = PearsonCorrCoef().to(device)

# 设定基本的存储路径
# 定义模型和损失函数
num_classes =5
loss_func = CombinedLoss(num_classes=5, alpha=0.8, beta=0.2,thegma =0.8)
def evaluate():
    model.eval()
    total_eval_accuracy = 0
    y_pred = torch.tensor([0, 0]).to(device)
    y_truth = torch.tensor([0, 0]).to(device)


    for batch in tqdm(dev_loader, desc="Evaluating"):
        input_ids = batch['input_ids'].to(device)
        attention_mask = batch['attention_mask'].to(device)
        labels = batch['labels'].to(device)

        with torch.no_grad():
            outputs = model(input_ids, attention_mask=attention_mask)

        logits = outputs.logits

        preds = torch.argmax(logits, dim=1)

        y_pred = torch.cat((y_pred, preds), dim=0)
        y_truth = torch.cat((y_truth, labels), dim=0)

        accuracy = (preds == labels).float().mean()
        total_eval_accuracy += accuracy.item()
        # # 计算每个类别的正确计数和总计数
        # for i in range(num_classes[0]):
        #     correct_count[i] = torch.sum((preds == labels) & (labels == emotion_groups[i]))
        #     total_count[i] = torch.sum(labels == emotion_groups[i])

        # 计算每个类别的正确率
        # 计算每个类别的出现比例作为权重
        # weights = total_count / total_count.sum()

        # 计算加权的每个类别的正确率
        # weighted_accuracy = correct_count / total_count * weights
        # weighted_accuracy_sum = sum(weighted_accuracy)

        # 打印结果
        # for i in range(num_classes[0]):
        #     if total_count[i] > 0:  # 检查是否有总计数，避免除以零的错误
        #         print(f"Weighted accuracy for class {i}: {weighted_accuracy[i].item():.2f}")
        #     else:
        #         print(f"Class {i} does not appear in the labels.")
    pearson_corr = pearson(y_pred.to(torch.float), y_truth.to(torch.float))
    average_eval_accuracy = total_eval_accuracy / len(dev_loader)
    
    result = pearson_corr.item()
    if  result > 0.63:
        preds_np = y_truth.detach().cpu().numpy()
        # 创建 DataFrame
        preds_df = pd.DataFrame(preds_np)
        # 保存为 CSV 文件
        preds_df.to_csv('/root/EmotionalPolarity_y_truth.csv', index=False)
        print("y_truth have been saved to 'EmotionalPolarity_y_truth.csv'")
        
        # 如果 logits 在 GPU 上，确保转移到 CPU 并转换为 numpy 数组
        logits_np = y_pred.detach().cpu().numpy()
        # 创建 DataFrame
        logits_df = pd.DataFrame(logits_np)
        # 保存为 TSV 文件
        logits_df.to_csv('EmotionalPolarity'+str(result)+'y_pred.csv', sep='\t', index=False)
        print("y_pred have been saved to 'EmotionalPolarity'"+str(result)+'y_pred.csv')

    return average_eval_accuracy, pearson_corr.item()

epochs = 10
for epoch in range(epochs):
    model.train()
    total_loss = 0

    for batch in tqdm(train_loader, desc="Epoch {}".format(epoch + 1)):
        
        input_ids = batch['input_ids'].to(device)
        attention_mask = batch['attention_mask'].to(device)
        labels = batch['labels'].to(device)

        optimizer.zero_grad()
        outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
        logits = outputs.logits
        loss = loss_func(logits, labels)
        loss.backward()
        optimizer.step()
        total_loss += loss.item()

    if (epoch+1) % 1 == 0:
        max_correlation = 0.62
        eval_res = evaluate()

        print("Dev eval result:", eval_res)
        if eval_res[1]>max_correlation:
            torch.save(model, '/root/models/EmotionalPolarity' + str(eval_res) + "-" + str(epoch) + '.pth')





