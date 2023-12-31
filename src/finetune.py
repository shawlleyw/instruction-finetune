import argparse
import os
import torch
import dataset
import hyper_params
from tokenizer import Tokenizer

import transformers
from transformers import get_linear_schedule_with_warmup

import colossalai
from colossalai.booster import Booster
from colossalai.booster.plugin import GeminiPlugin, LowLevelZeroPlugin, TorchDDPPlugin, TorchFSDPPlugin
from colossalai.cluster import DistCoordinator
from colossalai.nn.optimizer import HybridAdam

from peft import get_peft_model, LoraConfig, TaskType
from tqdm import tqdm
import utils

def train_epoch(epoch, total_epoch, model, optimizer, lr_sched, dataloader, booster, coord):
    model.train()
    with tqdm(dataloader, desc=f"epoch: {epoch + 1} / {total_epoch}", disable=not coord.is_master()) as pbar:
        for batch in pbar:
            #torch.cuda.empty_cache()
            batch = {k: v.cuda() for k, v in batch.items()}
            #print(batch['labels'].size())
            outputs = model(**batch)
            loss = outputs.loss
            booster.backward(loss, optimizer)
            optimizer.step()
            optimizer.zero_grad()
            lr_sched.step()
            pbar.set_postfix({'loss': loss.item()})
            
def LoRAWapper(model):
    peft_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM, inference_mode=False, r=8, lora_alpha=32, lora_dropout=0.1
    )
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()
    return model
    
def train(args):
    
    colossalai.launch_from_torch(config={}, seed=42)
    coordinator = DistCoordinator()
    
    lr = hyper_params.LEARNING_RATE * coordinator.world_size
    
    if args.plugin == "torch_ddp":
        plugin = TorchDDPPlugin()
    elif args.plugin == "gemini":
        plugin = GeminiPlugin(placement_policy='static', strict_ddp_mode=True)
    elif args.plugin == "low_level_zero":
        plugin = LowLevelZeroPlugin(stage=2, cpu_offload=True)
    elif args.plugin == "torch_fsdp":
        plugin = TorchFSDPPlugin()
    else:
        plugin = None
        
    booster = Booster(plugin=plugin)
    
    tokenizer = Tokenizer(args.model, args.max_tokens)
    pretrained_model = transformers.AutoModelForCausalLM.from_pretrained(args.model, device_map={'':torch.cuda.current_device()}, torch_dtype=torch.float16) 
    tokenizer.resize_model(pretrained_model)
    
    data = dataset.prepare_dataset(args.data_path, tokenizer)
    collator = dataset.DataCollator(
        input_pad=tokenizer.base_tokenizer.pad_token_id, 
        label_pad=tokenizer.IGNORE_INDEX, 
        tokenizer=tokenizer,
    )
    
    lora_model = LoRAWapper(pretrained_model)
    
    if plugin:
        dataloader = plugin.prepare_dataloader(data, batch_size=args.batch_size, collate_fn=collator, shuffle=True, drop_last=True)
    else:
       dataloader = torch.utils.data.dataloader.DataLoader(data, batch_size=args.batch_size, collate_fn=collator, shuffle=True, drop_last=True)

    optimizer = HybridAdam(lora_model.parameters(), lr=lr)
    total_steps = len(dataloader) * args.epoch
    lr_scheduler = get_linear_schedule_with_warmup(
        optimizer = optimizer,
        num_warmup_steps = hyper_params.WARMUP_RATE * total_steps, 
        num_training_steps = total_steps,
    )
    boost_model, optimizer, _, _, lr_scheduler = booster.boost(lora_model, optimizer=optimizer, lr_scheduler=lr_scheduler)
    
    with utils.MemoryTracker("Finetuning", args.memory_track):
        for epoch in range(args.epoch):
            train_epoch(epoch, args.epoch, boost_model, optimizer, lr_scheduler, dataloader, booster, coordinator)
        if coordinator.is_master():
            # booster.save_model(model, "./ckpt/model.pth")
            boost_model.unwrap().save_pretrained(args.ckpt)


def main():
    parser = argparse.ArgumentParser (
        prog="Instruction Finetune",
        description="Fine-tune LLM models in colossal-AI framework through QLoRA"
    )
    parser.add_argument("--model", type=str, dest="model", default="gpt2")
    parser.add_argument("--epoch", type=int, dest="epoch", default=1)
    parser.add_argument("--memory-track", action="store_true", dest="memory_track", default=False)
    parser.add_argument("--batch", type=int, dest="batch_size", default=4, help="specify batch size")
    parser.add_argument("--data", type=str, dest="data_path", required=True, help="specify data path")
    parser.add_argument("--tokens", type=int, dest="max_tokens", default=512, help="specify max tokens")
    parser.add_argument("--plugin", type=str, dest="plugin", default="torch_ddp", choices=["torch_ddp", 'gemini', 'low_level_zero', 'torch_fsdp'], help="specify a plugin")
    parser.add_argument("--ckpt", type=str, dest="ckpt", default=None, required=True, help="Path to save LoRA parameters")
    
    args = parser.parse_args()
    assert(not os.path.exists(args.ckpt))
    
    train(args)
    
if __name__ == '__main__':
    main()
