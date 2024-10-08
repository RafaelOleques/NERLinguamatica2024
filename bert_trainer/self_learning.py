import pandas as pd
from active_sampling import active_sampling

from flert import Training
#from model import Training
#from transformers import pipeline
from flert_pipeline import Pipeline

from configs import SBERT, SEED, SENTENCE_THRESHOLD, TERM_THRESHOLD, HISTOGRAM, LINEAR, FIXED
from transformers import AutoTokenizer, AutoModelForTokenClassification
from datasets import Dataset
from tqdm import tqdm
from transformers.pipelines.pt_utils import KeyDataset
import time
import json
import os
from os import path
from copy import deepcopy
import numpy as np
import shutil
from tools.txt2df import create_dataframe_from_txt

import sys
sys.stderr = sys.stdout


class SelfLearning:

    def __init__(self, input: str, output: str, 
                 percent_sampling_random: float, percent_sampling_dissimilar: float, 
                 min_size_random: int, min_size_dissimilar: int, 
                 labeled_corpus_path: str, unlabeled_corpus_path: str, 
                 sentence_embedding_name: str,
                 model_checkpoint: str, model_name: str,
                 corpus_name: str) -> None:
        
        self.percent_sampling_random = percent_sampling_random
        self.min_size_random = min_size_random
        self.percent_sampling_dissimilar = percent_sampling_dissimilar
        self.min_size_dissimilar = min_size_dissimilar
        self.sentence_embedding_name = sentence_embedding_name

        self.input = input
        self.output = output

        self.original_labeled_corpus = create_dataframe_from_txt(labeled_corpus_path, file="train.txt")
        self.labeled_corpus = deepcopy(self.original_labeled_corpus)


        with open(unlabeled_corpus_path, 'r') as f:
            unlabeled_data = json.load(f)

        self.unlabeled_corpus = pd.DataFrame(unlabeled_data)
        self.unlabeled_corpus["id"] = self.unlabeled_corpus.index


        #Remove sentences with one word
        count = self.unlabeled_corpus['sentences'].str.split().str.len()
        self.unlabeled_corpus = self.unlabeled_corpus[~(count<=1)].copy()

        self.model_checkpoint = model_checkpoint
        self.model_name = model_name

        self.corpus_name = corpus_name

    def set_trainer(self, max_length, truncation, lr, num_epochs, use_crf, use_rnn, main_evaluation_metric):
        self.max_length = max_length
        self.truncation = truncation
        self.lr = lr
        self.num_epochs = num_epochs
        self.use_crf = use_crf
        self.use_rnn = use_rnn
        self.main_evaluation_metric = main_evaluation_metric

    def pandas2txt(self, df, data_folder, output_dir_list):
        #To train
        with open(f"{data_folder}/train.txt", "w", encoding="utf-8") as f_out:
            for _, line in df.iterrows():
                for txt, tag in zip(line["tokens"], line["ner_tokens"]):
                    print("{} {}".format(txt, tag), file=f_out)
                print(file=f_out)

        #To analyse
        self._create_directory("generated_corpora")
        output_dir = deepcopy(output_dir_list)
        output_dir.insert(0, "generated_corpora")

        path = self._create_directory_recursive(".", output_dir)

        with open(f"{path}/train.txt", "w", encoding="utf-8") as f_out:
            for _, line in df.iterrows():
                for txt, tag in zip(line["tokens"], line["ner_tokens"]):
                    print("{} {}".format(txt, tag), file=f_out)
                print(file=f_out)

    def stop_iterations(self, actual_iteration: int, max_iterations: int, f1_patience: int, iteration_f1_without_increase: int) -> bool:
        """
        :param actual_iteration: number of the actual iteration
        :param max_iterations: max number of iterations
        :return boolean: true to end, false to continue
        """
        if actual_iteration == max_iterations:
            return True
        
        if self.unlabeled_corpus.size == 0:
            return True
        
        if iteration_f1_without_increase >= f1_patience:
            return True
        
        return False
    
    def ner_to_bio(self, entities: list, text: str):
        tokens = text.split(" ")
        # Inicializa a lista BIO com 'O' para cada token
        bio_tags = ['O'] * len(tokens)

        # Processa cada entidade detectada
        for entity in entities:
            start = entity['start']
            end = entity['end']
            entity_type = entity['entity']

            # Encontra o índice do token de início e fim baseado nos índices de caracteres
            token_start = None
            token_end = None
            current_pos = 0

            for i, token in enumerate(tokens):
                current_pos += len(token) + 1 #Considering space

                if current_pos > start and token_start is None:
                    token_start = i
                if current_pos >= end:
                    token_end = i
                    break

            if token_start is not None and token_end is not None:
                # Define o token de início como 'B-TYPE'
                bio_tags[token_start] = entity_type

                # Define os tokens seguintes como 'I-TYPE'
                for i in range(token_start + 1, token_end + 1):
                    bio_tags[i] = entity_type

        return bio_tags
    
    def threshold_filter_term(self, entities: list, scores: list, threshold: float):
        '''
        Threshold applied in each term
        '''
        return [entity if score >= threshold else "O" for entity, score in zip(entities, scores)]
    
    def threshold_filter_sentence(self, entities: dict, score: float, threshold: float):
        '''
        Threshold applied in the avg score of the sentence
        '''
        if score is not None and score >= threshold:
            return entities
        
        return []   

    def get_scores_sentence(self, scores):
        '''
        Implementar do por termo!
        '''
        if len(scores) > 0:
            return sum(scores)/len(scores)  
        else:
            return 0
            
    def sampling_annotation_instance(self, entities: dict, scores: list, threshold: float, threshold_level: int):
        #Threshold filter
        if threshold_level == SENTENCE_THRESHOLD:
            score_mean = self.get_scores_sentence(scores)
            entities_filtered = self.threshold_filter_sentence(entities, score_mean, threshold)
        elif threshold_level == TERM_THRESHOLD:
            entities_filtered = self.threshold_filter_term(entities, scores, threshold)
        
        #BIO annotation
        if entities_filtered == []:
            entities_filtered = ["O"] * len(entities)

        return entities_filtered
    
    def get_threshold(self, scores, threshold, threshold_function, iteration, bins=100):
        if threshold_function == HISTOGRAM:
            score_mean = [self.get_scores_sentence(score) for score in scores]
            return self.histogram_threshold(score_mean, threshold, bins)
        elif threshold_function == LINEAR:
            return self.linear_threshold(iteration)
        elif threshold_function == FIXED:
            return threshold
        
    def linear_threshold(self, iteration):
        return 1 - 0.005*(iteration+1)

    def histogram_threshold(self, scores, threshold, bins=100):
        values, indexes = np.histogram(scores, bins=bins)
        values = values[::-1]
        indexes = indexes[::-1]

        sum_values = 0
        end = -1

        for idx, value in enumerate(values):
            sum_values += value
            
            if sum_values >= self.min_size_dissimilar:
                end = idx
                break

        if end > -1:
            return indexes[end]

        #Otherwise, use the standard
        return threshold

    def apply_sampling_annotation(self, sample_patience: int, machine_annotated: pd.DataFrame, sampling: active_sampling, model_checkpoint: str, threshold: float, threshold_level: int, threshold_function: str, iteration: int):
        def filter(tokens):
            return tokens != ['O'] * len(tokens)
        
        for plus_seed in range(sample_patience):
            #Getting examples
            print("Sampling...")
            machine_annotated = sampling.random_dissimilarity(self.labeled_corpus, self.unlabeled_corpus, self.input, SEED+plus_seed, self.percent_sampling_random, self.percent_sampling_dissimilar, self.min_size_random, self.min_size_dissimilar)

            #Instance model
            pipe = Pipeline(model_checkpoint)
            
            #Getting predictions
            print("Geting predicitions...")
            dataset = Dataset.from_pandas(machine_annotated)
            
            #Computing scores
            entities_scores = [pipe.get_prediction(sentence) for idx, sentence in enumerate(tqdm(KeyDataset(dataset, "sentences")))]
            entities_list = [entities["entities"] for entities in entities_scores]

            scores = [entities["scores"] for entities in entities_scores]
            #scores_numeric = [score for score in scores if score is not None]

            #Getting threshold by histogram
            threshold_dynamic = self.get_threshold(scores, threshold, threshold_function, iteration)
            print("==========> threshold_dynamic:", threshold_dynamic)

            predictions = [self.sampling_annotation_instance(entities, score, threshold_dynamic, threshold_level) for entities, score in zip(entities_list, scores)]
            machine_annotated["ner_tokens"] = predictions

            #Remove instances that only have "O"
            machine_annotated = machine_annotated[machine_annotated['ner_tokens'].apply(lambda x: filter(x))]

            #Tokenize sentence
            machine_annotated["tokens"] = [sentence.split(" ") for sentence in machine_annotated["sentences"].values]

            #Remove the machine annotated sentences of the unlabeled corpus
            ids_to_remove = machine_annotated['id']
            self.unlabeled_corpus = self.unlabeled_corpus[~self.unlabeled_corpus['id'].isin(ids_to_remove)]
            
            #Check if any example was annotated
            if len(machine_annotated) == 0:
                continue 
            else:
                break
        
        return machine_annotated
    
    def _create_directory(self, ref):
        if path.exists(ref) == False:
            os.mkdir(ref)

    def _create_directory_recursive(self, root, dir_list):
        path = root

        for dir in dir_list:
            path = "{path}/{dir}".format(path=path, dir=dir)
            self._create_directory(path)

        return path
    
    def save_json(self, root, dir_list, json_object, file_name):
        path = self._create_directory_recursive(root, dir_list)

        with open("{path}/{file_name}".format(path=path, file_name=file_name), "w", encoding='utf-8') as outfile:
            outfile.write(json_object)

    def iterations(self, data_folder: str, max_iterations: int, sample_patience: int, threshold: float, threshold_level: str, f1_increase: float, f1_patience: int, threshold_function: str, folds, fold, output_dir_list):
        """
        :param max_iterations: max number of iterations
        :param sample_patience: tentatives of new samplings with different seeds
        :param threshold: threshold to be applied
        :param threshold_level: SENTENCE_THRESHOLD or TERM_THRESHOLD from configs
        """
        start_time = time.time()
        best_f1 = 0 #todo
        iteration_f1_without_increase = 0

        sampling = active_sampling(self.sentence_embedding_name)
        model_checkpoint = self.model_checkpoint
        
        '''#########################################################################
        actual_iteration = 0
        output_dir_list = output_dir_list + [threshold_level, threshold_function, str(threshold), f"random_{self.percent_sampling_random}", str(actual_iteration)]

        generated_dir = deepcopy(output_dir_list)
        generated_dir.insert(0, "models")
        trained_checkpoint = self._create_directory_recursive(".", generated_dir)
        
        print("Sampling...")
        machine_annotated = pd.DataFrame({self.input: [], self.output: []})
        machine_annotated = self.apply_sampling_annotation(sample_patience, machine_annotated, sampling, trained_checkpoint, threshold, threshold_level, threshold_function, 0)
        print(machine_annotated)
        
        #Concat labeled corpus and machine annotated
        print("Merging machine annotated examples...")
        print("Labeled corpus -------->",  len(self.labeled_corpus.ner_tokens.values))
        print("Machine annotated -------->", machine_annotated.columns.values, len(machine_annotated.index), len(machine_annotated.ner_tokens.values))
        self.labeled_corpus = pd.concat([self.labeled_corpus, machine_annotated], ignore_index=True)

        #Pandas df to txt
        self.pandas2txt(self.labeled_corpus, data_folder)

        print("Labeled corpus + Machine annotated -------->", self.labeled_corpus.columns.values, len(self.labeled_corpus.index), len(self.labeled_corpus.ner_tokens.values))

        #Saving new training set
        generated_dir = deepcopy(output_dir_list)
        generated_dir.insert(0, "generated_corpora")
        self.save_json(".", generated_dir, self.labeled_corpus.to_json(orient="records"), "train.json")'''
        #########################################################################

        for actual_iteration in range(0, max_iterations):
            print("=======> Iteration {actual_iteration}".format(actual_iteration=actual_iteration))
            output_dir_list = output_dir_list + [threshold_level, threshold_function, str(threshold), f"random_{self.percent_sampling_random}", str(actual_iteration)]
            machine_annotated = pd.DataFrame({self.input: [], self.output: []})

            #Training model
            print("Training...")
            print("Begin -------->", self.labeled_corpus.columns.values, len(self.labeled_corpus.index), len(self.labeled_corpus.ner_tokens.values))
            #training = Training(data_folder, self.corpus_name, model_checkpoint, self.model_name, max_length, padding, truncation, lr, batch_size, num_epochs, weight_decay, output_dir_list, self.labeled_corpus, model_layer=model_layer)
            training = Training(data_folder, self.corpus_name, model_checkpoint, self.model_name, output_dir_list)
            training.train(self.max_length, self.truncation, self.lr, self.num_epochs, self.use_crf, self.use_rnn, self.main_evaluation_metric)

            print("Saving metrics...")
            y_probs, metrics = training.get_and_save_metrics_test()
        
            #Getting f1
            if metrics["macro avg"]["f1-score"] - best_f1 > f1_increase:
                iteration_f1_without_increase = 0
            else:
                iteration_f1_without_increase += 1

            if metrics["macro avg"]["f1-score"] > best_f1:
                best_f1 = metrics["macro avg"]["f1-score"]            

            #break

            #Stop criterium
            if self.stop_iterations(actual_iteration, max_iterations, f1_patience, iteration_f1_without_increase):
                break

            #Last trained as new checkpoint
            generated_dir = deepcopy(output_dir_list)
            generated_dir.insert(0, "models")
            trained_checkpoint = self._create_directory_recursive(".", generated_dir)

            #Labeling
            print("Labeling...")
            machine_annotated = self.apply_sampling_annotation(sample_patience, machine_annotated, sampling, trained_checkpoint, threshold, threshold_level, threshold_function, actual_iteration)
            print(machine_annotated)

            #Check if don't have any machine annotated sentence
            if len(machine_annotated) == 0:
                break
            
            #Concat labeled corpus and machine annotated
            print("Merging machine annotated examples...")
            print("Labeled corpus -------->",  len(self.labeled_corpus.ner_tokens.values))
            print("Machine annotated -------->", machine_annotated.columns.values, len(machine_annotated.index), len(machine_annotated.ner_tokens.values))
            self.labeled_corpus = pd.concat([self.labeled_corpus, machine_annotated], ignore_index=True)

            #Pandas df to txt
            self.pandas2txt(self.labeled_corpus, data_folder, output_dir_list)

            print("Labeled corpus + Machine annotated -------->", self.labeled_corpus.columns.values, len(self.labeled_corpus.index), len(self.labeled_corpus.ner_tokens.values))

            #Saving new training set
            generated_dir = deepcopy(output_dir_list)
            generated_dir.insert(0, "generated_corpora")
            self.save_json(".", generated_dir, self.labeled_corpus.to_json(orient="records"), "train.json")
        
        print("--- %s seconds ---" % (time.time() - start_time))

        return output_dir_list
