#!/usr/bin/env python3
# coding:utf-8

# default libraries
import time
from _io import TextIOWrapper
import logging
import tempfile
import subprocess
import argparse
from collections import defaultdict, Counter
from typing import List, Tuple, Set, Dict, IO, Iterable
from pathlib import Path

from tqdm import tqdm

# local libraries
from ppanggolin.formats import check_pangenome_info
from ppanggolin.geneFamily import GeneFamily
from ppanggolin.utils import mk_outdir, read_compressed_or_not, create_tmpdir
from ppanggolin.pangenome import Pangenome
from ppanggolin.region import Spot
from ppanggolin.figures.draw_spot import draw_selected_spots, subgraph
from ppanggolin.formats.readBinaries import get_non_redundant_gene_sequences_from_file


def create_mmseqs_db(seq_files: Iterable[Path], tmpdir: Path, basename="sequences") -> Path:
    """
    Create a MMseqs2 sequence database with the given fasta files.

    :param seq_files: An iterable of path of FASTA files.
    :param tmpdir: Path to the temporary directory where the database will be created.
    :param basename: Prefix for the database file (default: "sequences").

    :return: Path to the created MMseqs2 database file.
    """

    with tempfile.NamedTemporaryFile(mode="w", dir=tmpdir, delete=False, suffix=".DB", prefix=basename) as seqdb:
        cmd = ["mmseqs", "createdb"] + [seq_file.as_posix() for seq_file in seq_files] + [seqdb.name, '--dbtype', '0']

        logging.getLogger("PPanGGOLiN").debug(" ".join(cmd))
        subprocess.run(cmd, stdout=subprocess.DEVNULL)

    return Path(seqdb.name)


def translate_with_mmseqs(seqdb: Path, translation_table: int, cpu: int, tmpdir: Path) -> Path:
    """
    Translate nucleotide sequences in an MMseqs2 sequence database to amino acid sequences.

    :param seqdb: Path to the input MMseqs2 sequence database containing nucleotide sequences.
    :param translation_table: The translation table to use for conversion.
    :param cpu: Number of CPU cores to use for translation.
    :param tmpdir: Path to the temporary directory for intermediate files.

    :return: Path to the new MMseqs2 sequence database containing translated amino acid sequences.
    """

    with tempfile.NamedTemporaryFile(mode="w", dir=tmpdir, delete=False, prefix=seqdb.stem,
                                     suffix=".aa.DB") as seqdb_aa:
        cmd = ["mmseqs", "translatenucs", seqdb.as_posix(), seqdb_aa.name, "--translation-table",
               f"{translation_table}", "--threads", str(cpu)]

        logging.getLogger().debug(" ".join(cmd))
        subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)

    return Path(seqdb_aa.name)


def align_seq_to_pang(target_seq_file: Path, query_seq_files: Iterable[Path],
                      tmpdir: Path, cpu: int = 1, no_defrag: bool = False,
                      identity: float = 0.8, coverage: float = 0.8,
                      is_query_nt: bool = False, is_target_nt: bool = False, translation_table: int = None) -> Path:
    """
    Align fasta sequence to pangenome sequences. 

    :param target_seq_file: File with sequences of pangenome (target)
    :param query_seq_files: Iterable of files with sequences from input file (query)
    :param tmpdir: Temporary directory to align sequences
    :param cpu: Number of available cpu
    :param no_defrag: Do not apply defragmentation
    :param identity: minimal identity threshold for the alignment
    :param coverage: minimal identity threshold for the alignment
    :param is_query_nt: Is the sequence file (query) are nucleotide sequences. If True, sequences are translated by mmseqs
    :param is_target_nt: Is the sequences of pangenome (target) are nucleotide sequences. If True, sequences are translated by mmseqs
    :param translation_table: Translation table to use, if sequences are nucleotide and need to be translated.

    :return: Alignement result file
    """

    target_db = create_mmseqs_db([target_seq_file], tmpdir, basename="target_sequences")
    query_db = create_mmseqs_db(query_seq_files, tmpdir, basename="query_sequences")

    if is_target_nt:
        logging.getLogger().debug(
            f"Target sequences will be translated by mmseqs with translation table {translation_table}")
        target_db = translate_with_mmseqs(target_db, translation_table, cpu, tmpdir)

    if is_query_nt:
        logging.getLogger().debug(
            f"Query sequences will be translated by mmseqs with translation table {translation_table}")
        query_db = translate_with_mmseqs(query_db, translation_table, cpu, tmpdir)

    cov_mode = "2"  # coverage of query
    if no_defrag:
        cov_mode = "0"  # coverage of query and target

    # mmseqs search command
    # see https://github.com/soedinglab/MMseqs2/issues/373 Using a combination of param to no miss short proteins 

    with tempfile.NamedTemporaryFile(mode="w", dir=tmpdir.as_posix(), prefix="aln_result_db_file", suffix=".aln.DB",
                                     delete=False) as aln_db:
        cmd = ["mmseqs", "search", query_db.as_posix(), target_db.as_posix(), aln_db.name, tmpdir.as_posix(), "-a",
               "--min-seq-id", str(identity),
               "-c", str(coverage), "--cov-mode", cov_mode, "--threads", str(cpu),
               "--seed-sub-mat", "VTML40.out", "-s", "2", '--comp-bias-corr', "0", "--mask", "0", "-e", "1"]

        logging.getLogger().info("Aligning sequences")
        logging.getLogger().debug(" ".join(cmd))

        start = time.time()
        subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)
        align_time = time.time() - start
        logging.getLogger().info(f"Done aligning sequences in {round(align_time, 2)} seconds")

        with tempfile.NamedTemporaryFile(mode="w", dir=tmpdir, prefix="aln_result_db_file", suffix=".tsv",
                                         delete=False) as outfile:
            cmd = ["mmseqs", "convertalis", query_db.as_posix(), target_db.as_posix(), aln_db.name, outfile.name,
                   "--format-mode", "2"]

            logging.getLogger().info("Extracting alignments...")
            logging.getLogger().debug(" ".join(cmd))
            subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)

    return Path(outfile.name)


def map_input_gene_to_family_all_aln(aln_res: Path, outdir: Path,
                                     pangenome: Pangenome) -> Tuple[Dict[str, GeneFamily], Path]:
    """
    Read alignment result to link input sequences to pangenome gene family. 
    Alignment have been made against all genes of the pangenome.

    :param aln_res: Alignement result file
    :param outdir: Output directory
    :param pangenome: Input pangenome

    :return: Dictionnary with sequence link to pangenome gene families and actual path to the cleaned alignment file
    """

    seq2pang = {}
    aln_file_clean = outdir / "alignment_input_seqs_to_all_pangenome_genes.tsv"  # write the actual result file
    logging.getLogger().debug(f'Writing alignment file in {aln_file_clean}')

    with open(aln_res, "r") as alnFile, open(aln_file_clean, "w") as aln_outfl:
        for line in alnFile:
            line_splitted = line.split()

            line_splitted[1] = line_splitted[1].replace("ppanggolin_", "")  # remove the 'ppanggolin_' bit of the id
            line_splitted[0] = line_splitted[0].replace("ppanggolin_", "")

            input_seq_id, gene_id = line_splitted[0:2]

            aln_outfl.write("\t".join(line_splitted) + "\n")

            if seq2pang.get(input_seq_id) is None:  # if no results were found yet
                family = pangenome.get_gene(gene_id).family
                seq2pang[input_seq_id] = family  # then the best hit is the first one we see.

    return seq2pang, aln_file_clean


def map_input_gene_to_family_rep_aln(aln_res: Path, outdir: Path,
                                     pangenome: Pangenome) -> Tuple[Dict[str, GeneFamily], str]:
    """
    Read alignment result to link input sequences to pangenome gene family.
    Alignment have been made against representative sequence of gene families of the pangenome.

    :param aln_res: Alignement result file
    :param outdir: Output directory
    :param pangenome: Input pangenome

    :return: Dictionnary with sequence link to pangenome gene families and actual path to the cleaned alignment file
    """
    seq2pang = {}
    aln_file_clean = outdir / "alignment_input_seqs_to_pangenome_gene_families.tsv"  # write the actual result file

    logging.getLogger().debug(f'Writing alignment file in {aln_file_clean}')

    with open(aln_res, "r") as alnFile, open(aln_file_clean, "w") as aln_outfl:
        for line in alnFile:
            line_splitted = line.split()

            line_splitted[1] = line_splitted[1].replace("ppanggolin_", "")  # remove the 'ppanggolin_' bit of the id
            line_splitted[0] = line_splitted[0].replace("ppanggolin_", "")

            aln_outfl.write("\t".join(line_splitted) + "\n")

            input_seq_id, gene_family_id = line_splitted[0:2]

            if seq2pang.get(input_seq_id) is None:  # if no results were found yet
                family = pangenome.get_gene_family(gene_family_id)  # then the best hit is the first one we see.
                seq2pang[input_seq_id] = family

    return seq2pang, aln_file_clean


def get_seq_ids(seq_file: TextIOWrapper) -> Tuple[Set[str], bool]:
    """
    Get sequence IDs from a sequence input file in FASTA format and guess the sequence type based on the first sequences.

    :param seq_file: A file object containing sequences in FASTA format.

    :return: A tuple containing a set of sequence IDs and a boolean indicating if the sequences are nucleotide sequences.
    """
    dna_expected_char = {'A', 'T', 'G', 'C', 'N'}
    seq_set = set()
    seq_count = 0
    first_seq_concat = ""

    for line in seq_file:
        if line.startswith(">"):
            seq_set.add(line[1:].split()[0].strip())
            seq_count += 1
        elif seq_count <= 20:
            first_seq_concat += line.strip()

    char_counter = Counter(first_seq_concat)
    is_nucleotide = all(char in dna_expected_char for char in char_counter)

    return seq_set, is_nucleotide


def write_gene_fam_sequences(pangenome: Pangenome, file_obj: IO, add: str = "", disable_bar: bool = False):
    """
    Export the sequence of gene families

    :param pangenome: Pangenome containing families
    :param file_obj: Temporary file where sequences will be written
    :param add: Add prefix to sequence name
    :param disable_bar: disable progress bar
    """
    for fam in tqdm(pangenome.gene_families, unit="families", disable=disable_bar,
                    total=pangenome.number_of_gene_families):
        file_obj.write(">" + add + fam.name + "\n")
        file_obj.write(fam.sequence + "\n")
    # file_obj.flush()


def write_all_gene_sequences(pangenome: Pangenome, file_obj: IO, add: str = "", disable_bar: bool = False):
    """
    Export the sequence of pangenome genes

    :param pangenome: Pangenome containing genes
    :param file_obj: Temporary file where sequences will be written
    :param add: Add prefix to sequence name
    :param disable_bar: disable progress bar

    """

    if pangenome.status["geneSequences"] == "inFile":
        get_non_redundant_gene_sequences_from_file(pangenome.file, file_obj, add=add, disable_bar=disable_bar)
    else:
        # this should never happen if the pangenome has been properly checked before launching this function.
        raise Exception("The pangenome does not include gene sequences")


def project_and_write_partition(seqid_to_gene_family: Dict[str, GeneFamily], seq_set: Set[str], output: Path) -> Path:
    """
    Project the partition of each sequence from the input file and write them in a file

    :param seqid_to_gene_family: dictionnary which link sequence and pangenome gene family
    :param seq_set: input sequences
    :param output: Path of the output directory

    :return: Path to file which contain partition projection
    """

    partition_proj = output.absolute() / "sequences_partition_projection.tsv"
    with open(partition_proj, "w") as partProjFile:
        for input_seq, gene_fam in seqid_to_gene_family.items():
            partProjFile.write(input_seq + "\t" + gene_fam.named_partition + "\n")
        for remaining_seq in seq_set - seqid_to_gene_family.keys():
            partProjFile.write(remaining_seq + "\tcloud\n")  # if there is no hit, it's going to be cloud genes.
    return partition_proj


def write_gene_to_gene_family(seqid_to_gene_family: Dict[str, GeneFamily], seq_set: Set[str], output: Path) -> Path:
    """
    Write input gene to pangenome gene family.

    :param seqid_to_gene_family: dictionnary which links input sequence and pangenome gene family
    :param seq_set: input sequences
    :param output: Path of the output directory

    :return: Path to the file which contains gene to gene family projection results
    """

    gene_fam_map_file = output.absolute() / "gene_to_gene_family.tsv"
    with open(gene_fam_map_file, "w") as cluster_proj_file:
        for input_seq in seq_set:
            # get the seq gene family and if there is no hit, itself
            gene_family = seqid_to_gene_family.get(input_seq)
            if gene_family is None:
                gene_family_name = input_seq
            else:
                gene_family_name = gene_family.name
            cluster_proj_file.write(f"{input_seq}\t{gene_family_name}\n")

    return gene_fam_map_file


def get_fam_to_rgp(pangenome, multigenics: set) -> dict:
    """
    Associate families to the RGP they belong to, and those they are bordering

    :param pangenome: Input pangenome
    :param multigenics: multigenics families

    :return: Dictionnary link families to RGP
    """
    fam2rgp = defaultdict(list)
    for rgp in pangenome.regions:
        for fam in rgp.families:
            fam2rgp[fam].append(rgp.name)
        for fam in [gene.family for border in rgp.get_bordering_genes(pangenome.parameters["spot"]["set_size"],
                                                                      multigenics) for gene in border]:
            fam2rgp[fam].append(rgp.name)
    return fam2rgp


def get_fam_to_spot(pangenome: Pangenome, multigenics: Set[GeneFamily]) \
        -> Tuple[Dict[str, List[Spot]], Dict[str, List[Spot]]]:
    """
    Reads a pangenome object to link families and spots and indicate where each family is.

    :param pangenome: Input pangenome
    :param multigenics: multigenics families

    :return: Dictionary of family to RGP and family to spot
    """
    # those are to be replaced as spots should be stored in the pangenome, and in the h5.
    fam2spot = defaultdict(list)
    fam2border = defaultdict(list)
    for spot in pangenome.spots:
        fams = set()
        fams_border = set()
        for rgp in spot.regions:
            fams |= set(rgp.families)
            fams_border |= set([gene.family for border in  # Set of families in border of spot
                                rgp.get_bordering_genes(pangenome.parameters["spot"]["set_size"], multigenics)
                                for gene in border])
        for fam in fams:
            fam2spot[fam].append(spot)
        for fam in fams_border:
            fam2border[fam].append(spot)
    return fam2spot, fam2border


def add_spot_str(spot: Spot) -> str:
    # TODO define as self.__str__ in spot
    """
    allow to map spot set

    :param spot: spot which will be return

    :return: Str with spot ID
    """
    return "spot_" + str(spot.ID)


def draw_spot_gexf(spots: set, output: Path, multigenics: set, fam_to_mod: dict, set_size: int = 3):
    """
    Draw a gexf graph of the spot

    :param spots: spot find in the alignment between pangenome and input sequences
    :param output: Path of the output directory
    :param multigenics: multigenics families
    :param fam_to_mod: dictionnary which link families and modules
    :param set_size:
    """
    for spot in spots:
        fname = output / f"spot_{str(spot.ID)}.gexf"
        subgraph(spot, fname, set_size=set_size, multigenics=multigenics, fam_to_mod=fam_to_mod)


def get_seq_info(seq_to_pang: dict, pangenome: Pangenome, output: Path, draw_related: bool = False, disable_bar=False):
    """
    Get sequences information after alignment

    :param seq_to_pang: Alignment result
    :param pangenome: Pangenome which contain information
    :param output: Path of the output directory
    :param draw_related: Draw figures and graphs in a gexf format of spots associated to the input sequences
    :param disable_bar: disable progress bar
    :return:
    """
    logging.getLogger("PPanGGOLiN").info("Writing RGP and spot information related to hits in the pangenome")
    multigenics = pangenome.get_multigenics(pangenome.parameters["rgp"]["dup_margin"])

    finfo = open(output / "info_input_seq.tsv", "w")
    finfo.write("input\tfamily\tpartition\tspot_list_as_member\tspot_list_as_border\trgp_list\n")
    fam2rgp = get_fam_to_rgp(pangenome, multigenics)
    fam2spot, fam2border = get_fam_to_spot(pangenome, multigenics)
    spot_list = set()
    for seq, panfam in seq_to_pang.items():
        finfo.write(seq + '\t' + panfam.name + "\t" + panfam.named_partition + "\t" + ",".join(
            map(add_spot_str, fam2spot[panfam])) + "\t" + ",".join(
            map(add_spot_str, fam2border[panfam])) + "\t" + ','.join(fam2rgp[panfam]) + "\n")
        spot_list |= set(fam2spot[panfam])
        spot_list |= set(fam2border[panfam])
    finfo.close()
    if draw_related:
        drawn_spots = set()
        for spot in spot_list:
            if len(spot.get_uniq_ordered_set()) > 1:
                drawn_spots.add(spot)
        logging.getLogger("PPanGGOLiN").info(f"Drawing the {len(drawn_spots)} spots with more than 1 organization "
                                             f"related to hits of the input sequences...")
        draw_selected_spots(drawn_spots, pangenome, output, pangenome.parameters["spot"]["overlapping_match"],
                            pangenome.parameters["spot"]["exact_match_size"], pangenome.parameters["spot"]["set_size"],
                            disable_bar=disable_bar)

        fam2mod = {}  # fam2module
        if pangenome.status["modules"] != "No":
            for mod in pangenome.modules:
                for fam in mod.families:
                    fam2mod[fam] = f"module_{mod.ID}"

        draw_spot_gexf(drawn_spots, output, multigenics=multigenics, fam_to_mod=fam2mod)

    logging.getLogger("PPanGGOLiN").info(f"File listing RGP and spots where sequences of interest are located : "
                                         f"{output / 'info_input_seq.tsv'}")


def get_input_seq_to_family_with_rep(pangenome: Pangenome, sequence_files: Iterable[Path], output: Path,
                                     tmpdir: Path, is_input_seq_nt: bool, cpu: int = 1, no_defrag: bool = False,
                                     identity: float = 0.8, coverage: float = 0.8, translation_table: int = 11,
                                     disable_bar: bool = False) -> Tuple[Path, Dict[str, GeneFamily]]:
    """
    Assign gene families from a pangenome to input sequences.

    This function aligns input sequences to gene families in a pangenome using MMseqs2 and assigns them
    to appropriate gene families based on alignment results.

    :param pangenome: Annotated pangenome containing gene families.
    :param sequence_files: Iterable of paths of FASTA files containing input sequences to align.
    :param output: Path to the output directory where alignment results will be stored.
    :param tmpdir: Temporary directory for intermediate files.
    :param is_input_seq_nt: Is input sequence file nucleotide sequences. 
    :param cpu: Number of CPU cores to use for the alignment (default: 1).
    :param no_defrag: If True, the defragmentation workflow is skipped (default: False).
    :param identity: Minimum identity threshold for the alignment (default: 0.8).
    :param coverage: Minimum coverage threshold for the alignment (default: 0.8).
    :param translation_table: Translation table to use if sequences need to be translated (default: 11).
    :param disable_bar: If True, disable the progress bar.
    
    :return: A tuple containing the path to the alignment result file, 
             and a dictionary mapping input sequences to gene families.

    """
    # delete False to be able to keep tmp file. If they are not keep tmpdir will be destroyed so no need to delete tmpfile
    with tempfile.NamedTemporaryFile(mode="w", dir=tmpdir.as_posix(), delete=False,
                                     prefix="representative_genes", suffix=".faa") as tmp_pang_file:
        logging.getLogger().debug(f'Write gene family sequences in {tmp_pang_file.name}')
        write_gene_fam_sequences(pangenome, tmp_pang_file, add="ppanggolin_", disable_bar=disable_bar)

        align_file = align_seq_to_pang(target_seq_file=Path(tmp_pang_file.name), query_seq_files=sequence_files,
                                       tmpdir=tmpdir, cpu=cpu,
                                       no_defrag=no_defrag, identity=identity, coverage=coverage,
                                       is_query_nt=is_input_seq_nt, is_target_nt=False,
                                       translation_table=translation_table)

        seq2pang, align_file = map_input_gene_to_family_rep_aln(align_file, output, pangenome)

    return align_file, seq2pang


def get_input_seq_to_family_with_all(pangenome: Pangenome, sequence_files: Iterable[Path], output: Path,
                                     tmpdir: Path, is_input_seq_nt: bool, cpu: int = 1, no_defrag: bool = False,
                                     identity: float = 0.8, coverage: float = 0.8, translation_table: int = 11,
                                     disable_bar: bool = False) -> Tuple[Path, Dict[str, GeneFamily]]:
    """
    Assign gene families from a pangenome to input sequences.

    This function aligns input sequences to all genes of the pangenome using MMseqs2 and assigns them
    to a gene families based on alignment results.

    :param pangenome: Annotated pangenome containing genes.
    :param sequence_files: Iterable of paths of FASTA files containing input sequences to align.
    :param output: Path to the output directory where alignment results will be stored.
    :param tmpdir: Temporary directory for intermediate files.
    :param is_input_seq_nt: Is input sequence file nucleotide sequences. 
    :param cpu: Number of CPU cores to use for the alignment (default: 1).
    :param no_defrag: If True, the defragmentation workflow is skipped (default: False).
    :param identity: Minimum identity threshold for the alignment (default: 0.8).
    :param coverage: Minimum coverage threshold for the alignment (default: 0.8).
    :param translation_table: Translation table to use if sequences need to be translated (default: 11).
    :param disable_bar: If True, disable the progress bar.

    :return: A tuple containing the path to the alignment result file, 
             and a dictionary mapping input sequences to gene families.
    """

    with tempfile.NamedTemporaryFile(mode="w", dir=tmpdir.as_posix(), delete=False,
                                     prefix="all_pangenome_genes", suffix=".fna") as tmp_pang_file:
        logging.getLogger().debug(f'Write all pangenome gene sequences in {tmp_pang_file.name}')
        write_all_gene_sequences(pangenome, tmp_pang_file, add="ppanggolin_", disable_bar=disable_bar)

        align_file = align_seq_to_pang(target_seq_file=Path(tmp_pang_file.name), query_seq_files=sequence_files,
                                       tmpdir=tmpdir, cpu=cpu,
                                       no_defrag=no_defrag, identity=identity, coverage=coverage,
                                       is_query_nt=is_input_seq_nt, is_target_nt=True,
                                       translation_table=translation_table)

        seq2pang, align_file = map_input_gene_to_family_all_aln(align_file, output, pangenome)

    return align_file, seq2pang


def align(pangenome: Pangenome, sequence_file: Path, output: Path, identity: float = 0.8,
          coverage: float = 0.8, no_defrag: bool = False, cpu: int = 1, getinfo: bool = False,
          use_representatives: bool = False, draw_related: bool = False, translation_table: int = 11, 
          tmpdir: Path = None, disable_bar: bool = False, keep_tmp=False):
    """
    Aligns pangenome sequences with sequences in a FASTA file using MMSeqs2.

    :param pangenome: Pangenome object containing gene families to align with the input sequences.
    :param sequence_file: Path to a FASTA file containing sequences to align with the pangenome.
    :param output: Path to the output directory.
    :param identity: Minimum identity threshold for the alignment.
    :param coverage: Minimum coverage threshold for the alignment.
    :param no_defrag: If True, the defrag workflow will not be used.
    :param cpu: Number of CPU cores to use.
    :param getinfo: If True, extract information related to the best hit of each query, such as the RGP it is in or the spots.
    :param use_representatives: If True, use representative sequences of gene families rather than all sequences to align input genes.
    :param draw_related: If True, draw figures and graphs in a gexf format of spots associated with the input sequences.
    :param translation_table: Translation table ID for nucleotide sequences.
    :param tmpdir: Temporary directory for intermediate files.
    :param disable_bar: If True, disable the progress bar.
    :param keep_tmp: If True, keep temporary files.
    """

    tmpdir = Path(tempfile.gettempdir()) if tmpdir is None else tmpdir
    if pangenome.status["geneFamilySequences"] not in ["inFile", "Loaded", "Computed"]:
        raise Exception("Cannot use this function as your pangenome does not have gene families representatives "
                        "associated to it. For now this works only if the clustering is realised by PPanGGOLiN.")
    # could be possible either by picking a representative somehow, or by aligning on genes rather than on
    # families, if they are in the pangenome.

    if getinfo or draw_related:
        need_mod = False
        if pangenome.status["modules"] != "No":
            # modules are not required to be loaded, but if they have been computed we load them.
            need_mod = True
        check_pangenome_info(pangenome, need_annotations=True, need_families=True, need_partitions=True, need_rgp=True,
                             need_spots=True, need_modules=need_mod, disable_bar=disable_bar)
    else:
        check_pangenome_info(pangenome, need_families=True, disable_bar=disable_bar)

    with read_compressed_or_not(sequence_file) as seqFileObj:
        seq_set, is_nucleotide = get_seq_ids(seqFileObj)

    with create_tmpdir(main_dir=tmpdir, basename="align_input_seq_tmpdir", keep_tmp=keep_tmp) as new_tmpdir:

        if use_representatives:
            align_file, seq2pang = get_input_seq_to_family_with_rep(pangenome, [sequence_file], output, new_tmpdir,
                                                                    is_input_seq_nt=is_nucleotide,
                                                                    cpu=cpu, no_defrag=no_defrag, identity=identity,
                                                                    coverage=coverage,
                                                                    translation_table=translation_table,
                                                                    disable_bar=disable_bar)
        else:
            align_file, seq2pang = get_input_seq_to_family_with_all(pangenome=pangenome, sequence_files=[sequence_file],
                                                                    output=output, tmpdir=new_tmpdir,
                                                                    is_input_seq_nt=is_nucleotide,
                                                                    cpu=cpu, no_defrag=no_defrag, identity=identity,
                                                                    coverage=coverage,
                                                                    translation_table=translation_table,
                                                                    disable_bar=disable_bar)

    if getinfo or draw_related:  # TODO Add getinfo to function and remove if
        get_seq_info(seq2pang, pangenome, output, draw_related, disable_bar=disable_bar)

    part_proj = project_and_write_partition(seq2pang, seq_set, output)  # write the partition assignation only
    logging.getLogger().info(f"sequences partition projection : '{part_proj}'")
    logging.getLogger().info(f"{len(seq2pang)} sequences over {len(seq_set)} have at least one hit in the pangenome.")
    logging.getLogger().info(f"Blast-tab file of the alignment : '{align_file}'")


def launch(args: argparse.Namespace):
    """
    Command launcher

    :param args: All arguments provide by user
    """
    mk_outdir(args.output, args.force)
    pangenome = Pangenome()
    pangenome.add_file(args.pangenome)
    align(pangenome=pangenome, sequence_file=args.sequences, output=args.output,
          tmpdir=args.tmpdir, identity=args.identity, coverage=args.coverage,
          no_defrag=args.no_defrag, cpu=args.cpu, getinfo=args.getinfo,
          use_representatives=args.fast, draw_related=args.draw_related,
          translation_table=args.translation_table,
          disable_bar=args.disable_prog_bar, keep_tmp=args.keep_tmp)


def subparser(sub_parser: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """
    Subparser to launch PPanGGOLiN in Command line

    :param sub_parser : sub_parser for align command

    :return : parser arguments for align command
    """
    parser = sub_parser.add_parser("align", formatter_class=argparse.RawTextHelpFormatter)
    parser_align(parser)
    return parser


def parser_align(parser: argparse.ArgumentParser):
    """
    Parser for specific argument of align command

    :param parser: parser for align argument
    """
    required = parser.add_argument_group(title="Required arguments",
                                         description="All of the following arguments are required :")
    required.add_argument('-S', '--sequences', required=False, type=Path,
                          help="sequences (nucleotides or amino acids) to align on the pangenome gene families")

    required.add_argument('-p', '--pangenome', required=False, type=Path, help="The pangenome .h5 file")
    required.add_argument('-o', '--output', required=True, type=Path,
                          help="Output directory where the file(s) will be written")

    optional = parser.add_argument_group(title="Optional arguments")
    optional.add_argument('--no_defrag', required=False, action="store_true",
                          help="DO NOT Realign gene families to link fragments with"
                               "their non-fragmented gene family. (default: False)")
    optional.add_argument('--identity', required=False, type=float, default=0.5,
                          help="min identity percentage threshold")
    optional.add_argument('--coverage', required=False, type=float, default=0.8,
                          help="min coverage percentage threshold")
    optional.add_argument("--fast", required=False, action="store_true",
                          help="Use representative sequences of gene families for input gene alignment. "
                               "This option is faster but may be less sensitive. By default, all pangenome genes are used.")
    optional.add_argument("--translation_table", required=False, default="11",
                          help="Translation table (genetic code) to use.")
    optional.add_argument("--getinfo", required=False, action="store_true",
                          help="Use this option to extract info related to the best hit of each query, "
                               "such as the RGP it is in, or the spots.")
    optional.add_argument("--draw_related", required=False, action="store_true",
                          help="Draw figures and provide graphs in a gexf format of the eventual spots"
                               " associated to the input sequences")
    # but does not use the option
    optional.add_argument("--use_pseudo", required=False, action="store_true",
                          help="In the context of provided annotation, use this option to read pseudogenes. "
                               "(Default behavior is to ignore them)")
    optional.add_argument("-c", "--cpu", required=False, default=1, type=int, help="Number of available cpus")
    optional.add_argument("--tmpdir", required=False, type=str, default=Path(tempfile.gettempdir()),
                          help="directory for storing temporary files")
    optional.add_argument("--keep_tmp", required=False, default=False, action="store_true",
                          help="Keeping temporary files (useful for debugging).")


if __name__ == '__main__':
    """To test local change and allow using debugger"""
    from ppanggolin.utils import set_verbosity_level, add_common_arguments

    main_parser = argparse.ArgumentParser(
        description="Depicting microbial species diversity via a Partitioned PanGenome Graph Of Linked Neighbors",
        formatter_class=argparse.RawTextHelpFormatter)
    parser_align(main_parser)
    add_common_arguments(main_parser)
    set_verbosity_level(main_parser.parse_args())
    launch(main_parser.parse_args())
