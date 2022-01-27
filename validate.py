#! /usr/bin/env python3
# Original code (2015) by Filip Ginter and Sampo Pyysalo.
# DZ 2018-11-04: Porting the validator to Python 3.
import fileinput
import sys
import io
import os.path
import argparse
import logging
import traceback
# According to https://stackoverflow.com/questions/1832893/python-regex-matching-unicode-properties,
# the regex module has the same API as re but it can check Unicode character properties using \p{}
# as in Perl.
#import re
import regex as re
import unicodedata
import json


THISDIR=os.path.dirname(os.path.realpath(os.path.abspath(__file__))) # The folder where this script resides.

# Constants for the column indices
COLCOUNT=10
ID,FORM,LEMMA,UPOS,XPOS,FEATS,HEAD,DEPREL,DEPS,MISC=range(COLCOUNT)
COLNAMES='ID,FORM,LEMMA,UPOS,XPOS,FEATS,HEAD,DEPREL,DEPS,MISC'.split(',')
TOKENSWSPACE=MISC+1 # one extra constant
AUX=MISC+2 # another extra constant
COP=MISC+3 # another extra constant

# Global variables:
curr_line = 0 # Current line in the input file
comment_start_line = 0 # The line in the input file on which the current sentence starts, including sentence-level comments.
sentence_line = 0 # The line in the input file on which the current sentence starts (the first node/token line, skipping comments)
sentence_id = None # The most recently read sentence id
line_of_first_enhanced_graph = None
line_of_first_tree_without_enhanced_graph = None
line_of_first_enhancement = None # any difference between non-empty DEPS and HEAD:DEPREL
line_of_first_empty_node = None
line_of_first_enhanced_orphan = None
line_of_global_entity = None
global_entity_attribute_string = None # to be able to check that repeated declarations are identical
entity_attribute_number = 0 # to be able to check that an entity does not have extra attributes
entity_attribute_index = {} # key: entity attribute name; value: the index of the attribute in the entity attribute list
entity_types = {} # key: entity (cluster) id; value: tuple: (type of the entity, identity (Wikipedia etc.), line of the first mention)
open_entity_mentions = [] # items are dictionaries with entity mention information
open_discontinuous_mentions = {} # key: entity id; describes last part of a discontinuous mention of that entity; item is dict, its keys: last_ipart, npart, line
entity_ids_this_document = {}
entity_ids_other_documents = {}
entity_bridge_relations = {} # key: srceid<tgteid pair; value: type of the entity (may be empty)
entity_split_antecedents = {} # key: tgteid; value: sorted list of srceids, serialized to string
error_counter = {} # key: error type value: error count
warn_on_missing_files = set() # langspec files which you should warn about in case they are missing (can be deprel, edeprel, feat_val, tokens_w_space)
warn_on_undoc_feats = '' # filled after reading docfeats.json; printed when an unknown feature is encountered in the data
warn_on_undoc_deps = '' # filled after reading docdeps.json; printed when an unknown relation is encountered in the data
warn_on_undoc_edeps = '' # filled after reading edeprels.json; printed when an unknown enhanced relation is encountered in the data
spaceafterno_in_effect = False # needed to check that no space after last word of sentence does not co-occur with new paragraph or document
featdata = {} # key: language code (feature-value-UPOS data loaded from feats.json)
auxdata = {} # key: language code (auxiliary/copula data loaded from data.json)
depreldata = {} # key: language code (deprel data loaded from deprels.json)
edepreldata = {} # key: language code (edeprel data loaded from edeprels.json)

def warn(msg, error_type, testlevel=0, testid='some-test', lineno=True, nodelineno=0, nodeid=0):
    """
    Print the warning.
    If lineno is True, print the number of the line last read from input. Note
    that once we have read a sentence, this is the number of the empty line
    after the sentence, hence we probably do not want to print it.
    If we still have an error that pertains to an individual node, and we know
    the number of the line where the node appears, we can supply it via
    nodelineno. Nonzero nodelineno means that lineno value is ignored.
    If lineno is False, print the number and starting line of the current tree.
    """
    global curr_fname, curr_line, sentence_line, sentence_id, error_counter, tree_counter, args
    error_counter[error_type] = error_counter.get(error_type, 0)+1
    if not args.quiet:
        if args.max_err>0 and error_counter[error_type]==args.max_err:
            print(('...suppressing further errors regarding ' + error_type), file=sys.stderr)
        elif args.max_err>0 and error_counter[error_type]>args.max_err:
            pass #suppressed
        else:
            if len(args.input)>1: #several files, should report which one
                if curr_fname=="-":
                    fn="(in STDIN) "
                else:
                    fn="(in "+os.path.basename(curr_fname)+") "
            else:
                fn=""
            sent = ''
            node = ''
            # Global variable (last read sentence id): sentence_id
            # Originally we used a parameter sid but we probably do not need to override the global value.
            if sentence_id:
                sent = ' Sent ' + sentence_id
            if nodeid:
                node = ' Node ' + str(nodeid)
            if nodelineno:
                print("[%sLine %d%s%s]: [L%d %s %s] %s" % (fn, nodelineno, sent, node, testlevel, error_type, testid, msg), file=sys.stderr)
            elif lineno:
                print("[%sLine %d%s%s]: [L%d %s %s] %s" % (fn, curr_line, sent, node, testlevel, error_type, testid, msg), file=sys.stderr)
            else:
                print("[%sTree number %d on line %d%s%s]: [L%d %s %s] %s" % (fn, tree_counter, sentence_line, sent, node, testlevel, error_type, testid, msg), file=sys.stderr)

###### Support functions

def is_whitespace(line):
    return re.match(r"^\s+$", line)

def is_word(cols):
    return re.match(r"^[1-9][0-9]*$", cols[ID])

def is_multiword_token(cols):
    return re.match(r"^[1-9][0-9]*-[1-9][0-9]*$", cols[ID])

def is_empty_node(cols):
    return re.match(r"^[0-9]+\.[1-9][0-9]*$", cols[ID])

def parse_empty_node_id(cols):
    m = re.match(r"^([0-9]+)\.([0-9]+)$", cols[ID])
    assert m, 'parse_empty_node_id with non-empty node'
    return m.groups()

def shorten(string):
    return string if len(string) < 25 else string[:20]+'[...]'

def lspec2ud(deprel):
    return deprel.split(':', 1)[0]



#==============================================================================
# Level 1 tests. Only CoNLL-U backbone. Values can be empty or non-UD.
#==============================================================================

sentid_re=re.compile('^# sent_id\s*=\s*(\S+)$')
def trees(inp, tag_sets, args):
    """
    `inp` a file-like object yielding lines as unicode
    `tag_sets` and `args` are needed for choosing the tests

    This function does elementary checking of the input and yields one
    sentence at a time from the input stream.

    This function is a generator. The caller can call it in a 'for x in ...'
    loop. In each iteration of the caller's loop, the generator will generate
    the next sentence, that is, it will read the next sentence from the input
    stream. (Technically, the function returns an object, and the object will
    then read the sentences within the caller's loop.)
    """
    global curr_line, comment_start_line, sentence_line, sentence_id
    comments = [] # List of comment lines to go with the current sentence
    lines = [] # List of token/word lines of the current sentence
    corrupted = False # In case of wrong number of columns check the remaining lines of the sentence but do not yield the sentence for further processing.
    comment_start_line = None
    testlevel = 1
    testclass = 'Format'
    for line_counter, line in enumerate(inp):
        curr_line = line_counter+1
        if not comment_start_line:
            comment_start_line = curr_line
        line = line.rstrip(u"\n")
        if is_whitespace(line):
            testid = 'pseudo-empty-line'
            testmessage = 'Spurious line that appears empty but is not; there are whitespace characters.'
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            # We will pretend that the line terminates a sentence in order to avoid subsequent misleading error messages.
            if lines:
                if not corrupted:
                    yield comments, lines
                comments = []
                lines = []
                corrupted = False
                comment_start_line = None
        elif not line: # empty line
            if lines: # sentence done
                if not corrupted:
                    yield comments, lines
                comments=[]
                lines=[]
                corrupted = False
                comment_start_line = None
            else:
                testid = 'extra-empty-line'
                testmessage = 'Spurious empty line. Only one empty line is expected after every sentence.'
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        elif line[0]=='#':
            # We will really validate sentence ids later. But now we want to remember
            # everything that looks like a sentence id and use it in the error messages.
            # Line numbers themselves may not be sufficient if we are reading multiple
            # files from a pipe.
            match = sentid_re.match(line)
            if match:
                sentence_id = match.group(1)
            if not lines: # before sentence
                comments.append(line)
            else:
                testid = 'misplaced-comment'
                testmessage = 'Spurious comment line. Comments are only allowed before a sentence.'
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        elif line[0].isdigit():
            validate_unicode_normalization(line)
            if not lines: # new sentence
                sentence_line=curr_line
            cols=line.split(u"\t")
            if len(cols)!=COLCOUNT:
                testid = 'number-of-columns'
                testmessage = 'The line has %d columns but %d are expected. The contents of the columns will not be checked.' % (len(cols), COLCOUNT)
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                corrupted = True
            # If there is an unexpected number of columns, do not test their contents.
            # Maybe the contents belongs to a different column. And we could see
            # an exception if a column value is missing.
            else:
                lines.append(cols)
                validate_cols_level1(cols)
                if args.level > 1:
                    validate_cols(cols,tag_sets,args)
        else: # A line which is neither a comment nor a token/word, nor empty. That's bad!
            testid = 'invalid-line'
            testmessage = "Spurious line: '%s'. All non-empty lines should start with a digit or the # character." % (line)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    else: # end of file
        if comments or lines: # These should have been yielded on an empty line!
            testid = 'missing-empty-line'
            testmessage = 'Missing empty line after the last sentence.'
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            if not corrupted:
                yield comments, lines

###### Tests applicable to a single row indpendently of the others

def validate_unicode_normalization(text):
    """
    Tests that letters composed of multiple Unicode characters (such as a base
    letter plus combining diacritics) conform to NFC normalization (canonical
    decomposition followed by canonical composition).
    """
    normalized_text = unicodedata.normalize('NFC', text)
    if text != normalized_text:
        # Find the first unmatched character and include it in the report.
        firsti = -1
        firstj = -1
        inpfirst = ''
        nfcfirst = ''
        tcols = text.split("\t")
        ncols = normalized_text.split("\t")
        for i in range(len(tcols)):
            for j in range(len(tcols[i])):
                if tcols[i][j] != ncols[i][j]:
                    firsti = i
                    firstj = j
                    inpfirst = unicodedata.name(tcols[i][j])
                    nfcfirst = unicodedata.name(ncols[i][j])
                    break
            if firsti >= 0:
                break
        testlevel = 1
        testclass = 'Unicode'
        testid = 'unicode-normalization'
        testmessage = "Unicode not normalized: %s.character[%d] is %s, should be %s." % (COLNAMES[firsti], firstj, inpfirst, nfcfirst)
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)

whitespace_re=re.compile('.*\s',re.U)
whitespace2_re=re.compile('.*\s\s', re.U)
def validate_cols_level1(cols):
    """
    Tests that can run on a single line and pertain only to the CoNLL-U file
    format, not to predefined sets of UD tags.
    """
    testlevel = 1
    testclass = 'Format'
    # Some whitespace may be permitted in FORM, LEMMA and MISC but not elsewhere.
    for col_idx in range(MISC+1):
        if col_idx >= len(cols):
            break # this has been already reported in trees()
        # Must never be empty
        if not cols[col_idx]:
            testid = 'empty-column'
            testmessage = 'Empty value in column %s.' % (COLNAMES[col_idx])
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        else:
            # Must never have leading/trailing whitespace
            if cols[col_idx][0].isspace():
                testid = 'leading-whitespace'
                testmessage = 'Leading whitespace not allowed in column %s.' % (COLNAMES[col_idx])
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            if cols[col_idx][-1].isspace():
                testid = 'trailing-whitespace'
                testmessage = 'Trailing whitespace not allowed in column %s.' % (COLNAMES[col_idx])
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            # Must never contain two consecutive whitespace characters
            if whitespace2_re.match(cols[col_idx]):
                testid = 'repeated-whitespace'
                testmessage = 'Two or more consecutive whitespace characters not allowed in column %s.' % (COLNAMES[col_idx])
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    # These columns must not have whitespace
    for col_idx in (ID,UPOS,XPOS,FEATS,HEAD,DEPREL,DEPS):
        if col_idx >= len(cols):
            break # this has been already reported in trees()
        if whitespace_re.match(cols[col_idx]):
            testid = 'invalid-whitespace'
            testmessage = "White space not allowed in column %s: '%s'." % (COLNAMES[col_idx], cols[col_idx])
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    # Check for the format of the ID value. (ID must not be empty.)
    if not (is_word(cols) or is_empty_node(cols) or is_multiword_token(cols)):
        testid = 'invalid-word-id'
        testmessage = "Unexpected ID format '%s'." % cols[ID]
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)

##### Tests applicable to the whole tree

interval_re=re.compile('^([0-9]+)-([0-9]+)$',re.U)
def validate_ID_sequence(tree):
    """
    Validates that the ID sequence is correctly formed.
    Besides issuing a warning if an error is found, it also returns False to
    the caller so it can avoid building a tree from corrupt ids.
    """
    ok = True
    testlevel = 1
    testclass = 'Format'
    words=[]
    tokens=[]
    current_word_id, next_empty_id = 0, 1
    for cols in tree:
        if not is_empty_node(cols):
            next_empty_id = 1    # reset sequence
        if is_word(cols):
            t_id=int(cols[ID])
            current_word_id = t_id
            words.append(t_id)
            # Not covered by the previous interval?
            if not (tokens and tokens[-1][0]<=t_id and tokens[-1][1]>=t_id):
                tokens.append((t_id,t_id)) # nope - let's make a default interval for it
        elif is_multiword_token(cols):
            match=interval_re.match(cols[ID]) # Check the interval against the regex
            if not match: # This should not happen. The function is_multiword_token() would then not return True.
                testid = 'invalid-word-interval'
                testmessage = "Spurious word interval definition: '%s'." % cols[ID]
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                ok = False
                continue
            beg,end=int(match.group(1)),int(match.group(2))
            if not ((not words and beg >= 1) or (words and beg >= words[-1] + 1)):
                testid = 'misplaced-word-interval'
                testmessage = 'Multiword range not before its first word.'
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                ok = False
                continue
            tokens.append((beg,end))
        elif is_empty_node(cols):
            word_id, empty_id = (int(i) for i in parse_empty_node_id(cols))
            if word_id != current_word_id or empty_id != next_empty_id:
                testid = 'misplaced-empty-node'
                testmessage = 'Empty node id %s, expected %d.%d' % (cols[ID], current_word_id, next_empty_id)
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                ok = False
            next_empty_id += 1
    # Now let's do some basic sanity checks on the sequences
    wrdstrseq = ','.join(str(x) for x in words)
    expstrseq = ','.join(str(x) for x in range(1, len(words)+1)) # Words should form a sequence 1,2,...
    if wrdstrseq != expstrseq:
        testid = 'word-id-sequence'
        testmessage = "Words do not form a sequence. Got '%s'. Expected '%s'." % (wrdstrseq, expstrseq)
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
        ok = False
    # Check elementary sanity of word intervals.
    # Remember that these are not just multi-word tokens. Here we have intervals even for single-word tokens (b=e)!
    for (b, e) in tokens:
        if e<b: # end before beginning
            testid = 'reversed-word-interval'
            testmessage = 'Spurious token interval %d-%d' % (b,e)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            ok = False
            continue
        if b<1 or e>len(words): # out of range
            testid = 'word-interval-out'
            testmessage = 'Spurious token interval %d-%d (out of range)' % (b,e)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            ok = False
            continue
    return ok

def validate_token_ranges(tree):
    """
    Checks that the word ranges for multiword tokens are valid.
    """
    testlevel = 1
    testclass = 'Format'
    covered = set()
    for cols in tree:
        if not is_multiword_token(cols):
            continue
        m = interval_re.match(cols[ID])
        if not m: # This should not happen. The function is_multiword_token() would then not return True.
            testid = 'invalid-word-interval'
            testmessage = "Spurious word interval definition: '%s'." % cols[ID]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            continue
        start, end = m.groups()
        try:
            start, end = int(start), int(end)
        except ValueError:
            assert False, 'internal error' # RE should assure that this works
        if not start < end: ###!!! This was already tested above in validate_ID_sequence()! Should we remove it from there?
            testid = 'reversed-word-interval'
            testmessage = 'Spurious token interval %d-%d' % (start, end)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            continue
        if covered & set(range(start, end+1)):
            testid = 'overlapping-word-intervals'
            testmessage = 'Range overlaps with others: %s' % cols[ID]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        covered |= set(range(start, end+1))

def validate_newlines(inp):
    if inp.newlines and inp.newlines!='\n':
        testlevel = 1
        testclass = 'Format'
        testid = 'non-unix-newline'
        testmessage = 'Only the unix-style LF line terminator is allowed.'
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)



#==============================================================================
# Level 2 tests. Tree structure, universal tags and deprels. Note that any
# well-formed Feature=Valid pair is allowed (because it could be language-
# specific) and any word form or lemma can contain spaces (because language-
# specific guidelines may permit it).
#==============================================================================

###### Metadata tests #########

def validate_sent_id(comments, known_ids, lcode):
    testlevel = 2
    testclass = 'Metadata'
    matched=[]
    for c in comments:
        match=sentid_re.match(c)
        if match:
            matched.append(match)
        else:
            if c.startswith('# sent_id') or c.startswith('#sent_id'):
                testid = 'invalid-sent-id'
                testmessage = "Spurious sent_id line: '%s' Should look like '# sent_id = xxxxx' where xxxxx is not whitespace. Forward slash reserved for special purposes." % c
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    if not matched:
        testid = 'missing-sent-id'
        testmessage = 'Missing the sent_id attribute.'
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    elif len(matched)>1:
        testid = 'multiple-sent-id'
        testmessage = 'Multiple sent_id attributes.'
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    else:
        # Uniqueness of sentence ids should be tested treebank-wide, not just file-wide.
        # For that to happen, all three files should be tested at once.
        sid=matched[0].group(1)
        if sid in known_ids:
            testid = 'non-unique-sent-id'
            testmessage = "Non-unique sent_id attribute '%s'." % sid
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        if sid.count(u"/")>1 or (sid.count(u"/")==1 and lcode!=u"ud" and lcode!=u"shopen"):
            testid = 'slash-in-sent-id'
            testmessage = "The forward slash is reserved for special use in parallel treebanks: '%s'" % sid
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        known_ids.add(sid)

newdoc_re = re.compile('^#\s*newdoc(\s|$)')
newpar_re = re.compile('^#\s*newpar(\s|$)')
text_re = re.compile('^#\s*text\s*=\s*(.+)$')
def validate_text_meta(comments, tree):
    # Remember if SpaceAfter=No applies to the last word of the sentence.
    # This is not prohibited in general but it is prohibited at the end of a paragraph or document.
    global spaceafterno_in_effect
    global sentence_line
    testlevel = 2
    testclass = 'Metadata'
    newdoc_matched = []
    newpar_matched = []
    text_matched = []
    for c in comments:
        newdoc_match = newdoc_re.match(c)
        if newdoc_match:
            newdoc_matched.append(newdoc_match)
        newpar_match = newpar_re.match(c)
        if newpar_match:
            newpar_matched.append(newpar_match)
        text_match = text_re.match(c)
        if text_match:
            text_matched.append(text_match)
    if len(newdoc_matched) > 1:
        testid = 'multiple-newdoc'
        testmessage = 'Multiple newdoc attributes.'
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    if len(newpar_matched) > 1:
        testid = 'multiple-newpar'
        testmessage = 'Multiple newpar attributes.'
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    if (newdoc_matched or newpar_matched) and spaceafterno_in_effect:
        testid = 'spaceafter-newdocpar'
        testmessage = 'New document or paragraph starts when the last token of the previous sentence says SpaceAfter=No.'
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    if not text_matched:
        testid = 'missing-text'
        testmessage = 'Missing the text attribute.'
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    elif len(text_matched) > 1:
        testid = 'multiple-text'
        testmessage = 'Multiple text attributes.'
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    else:
        stext = text_matched[0].group(1)
        if stext[-1].isspace():
            testid = 'text-trailing-whitespace'
            testmessage = 'The text attribute must not end with whitespace.'
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        # Validate the text against the SpaceAfter attribute in MISC.
        skip_words = set()
        mismatch_reported = 0 # do not report multiple mismatches in the same sentence; they usually have the same cause
        iline = 0
        for cols in tree:
            if MISC >= len(cols):
                # This error has been reported elsewhere but we cannot check MISC now.
                continue
            if 'NoSpaceAfter=Yes' in cols[MISC]: # I leave this without the split("|") to catch all
                testid = 'nospaceafter-yes'
                testmessage = "'NoSpaceAfter=Yes' should be replaced with 'SpaceAfter=No'."
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
            if '.' in cols[ID]: # empty node
                if 'SpaceAfter=No' in cols[MISC]: # I leave this without the split("|") to catch all
                    testid = 'spaceafter-empty-node'
                    testmessage = "'SpaceAfter=No' cannot occur with empty nodes."
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                continue
            elif '-' in cols[ID]: # multi-word token
                beg,end=cols[ID].split('-')
                try:
                    begi,endi = int(beg),int(end)
                except ValueError as e:
                    # This error has been reported elsewhere.
                    begi,endi=1,0
                # If we see a multi-word token, add its words to an ignore-set - these will be skipped, and also checked for absence of SpaceAfter=No
                for i in range(begi, endi+1):
                    skip_words.add(str(i))
            elif cols[ID] in skip_words:
                if 'SpaceAfter=No' in cols[MISC]:
                    testid = 'spaceafter-mwt-node'
                    testmessage = "'SpaceAfter=No' cannot occur with words that are part of a multi-word token."
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                continue
            else:
                # Err, I guess we have nothing to do here. :)
                pass
            # So now we have either a multi-word token or a word which is also a token in its entirety.
            if not stext.startswith(cols[FORM]):
                if not mismatch_reported:
                    testid = 'text-form-mismatch'
                    testmessage = "Mismatch between the text attribute and the FORM field. Form[%s] is '%s' but text is '%s...'" % (cols[ID], cols[FORM], stext[:len(cols[FORM])+20])
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                    mismatch_reported=1
            else:
                stext=stext[len(cols[FORM]):] # eat the form
                if 'SpaceAfter=No' in cols[MISC].split("|"):
                    spaceafterno_in_effect = True
                else:
                    spaceafterno_in_effect = False
                    if args.check_space_after and (stext) and not stext[0].isspace():
                        testid = 'missing-spaceafter'
                        testmessage = "'SpaceAfter=No' is missing in the MISC field of node #%s because the text is '%s'." % (cols[ID], shorten(cols[FORM]+stext))
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                    stext=stext.lstrip()
            iline += 1
        if stext:
            testid = 'text-extra-chars'
            testmessage = "Extra characters at the end of the text attribute, not accounted for in the FORM fields: '%s'" % stext
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)

##### Tests applicable to a single row indpendently of the others

def validate_cols(cols, tag_sets, args):
    """
    All tests that can run on a single line. Done as soon as the line is read,
    called from trees() if level>1.
    """
    if is_word(cols) or is_empty_node(cols):
        validate_character_constraints(cols) # level 2
        validate_upos(cols, tag_sets) # level 2
        validate_features(cols, tag_sets, args) # level 2 and up (relevant code checks whether higher level is required)
    elif is_multiword_token(cols):
        validate_token_empty_vals(cols)
    # else do nothing; we have already reported wrong ID format at level 1
    if is_word(cols):
        validate_deprels(cols, tag_sets, args) # level 2 and up
    elif is_empty_node(cols):
        validate_empty_node_empty_vals(cols) # level 2
    if args.level > 3:
        validate_whitespace(cols, tag_sets) # level 4 (it is language-specific; to disallow everywhere, use --lang ud)

def validate_token_empty_vals(cols):
    """
    Checks that a multi-word token has _ empty values in all fields except MISC.
    This is required by UD guidelines although it is not a problem in general,
    therefore a level 2 test.
    """
    assert is_multiword_token(cols), 'internal error'
    for col_idx in range(LEMMA, MISC): # all columns except the first two (ID, FORM) and the last one (MISC)
        if cols[col_idx] != '_':
            testlevel = 2
            testclass = 'Format'
            testid = 'mwt-nonempty-field'
            testmessage = "A multi-word token line must have '_' in the column %s. Now: '%s'." % (COLNAMES[col_idx], cols[col_idx])
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)

def validate_empty_node_empty_vals(cols):
    """
    Checks that an empty node has _ empty values in HEAD and DEPREL. This is
    required by UD guidelines but not necessarily by CoNLL-U, therefore
    a level 2 test.
    """
    assert is_empty_node(cols), 'internal error'
    for col_idx in (HEAD, DEPREL):
        if cols[col_idx]!= '_':
            testlevel = 2
            testclass = 'Format'
            testid = 'mwt-nonempty-field'
            testmessage = "An empty node must have '_' in the column %s. Now: '%s'." % (COLNAMES[col_idx], cols[col_idx])
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)

# Ll ... lowercase Unicode letters
# Lm ... modifier Unicode letters (e.g., superscript h)
# Lo ... other Unicode letters (all caseless scripts, e.g., Arabic)
# M .... combining diacritical marks
# Underscore is allowed between letters but not at beginning, end, or next to another underscore.
edeprelpart_resrc = '[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(_[\p{Ll}\p{Lm}\p{Lo}\p{M}]+)*';
# There must be always the universal part, consisting only of ASCII letters.
# There can be up to three additional, colon-separated parts: subtype, preposition and case.
# One of them, the preposition, may contain Unicode letters. We do not know which one it is
# (only if there are all four parts, we know it is the third one).
# ^[a-z]+(:[a-z]+)?(:[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(_[\p{Ll}\p{Lm}\p{Lo}\p{M}]+)*)?(:[a-z]+)?$
edeprel_resrc = '^[a-z]+(:[a-z]+)?(:' + edeprelpart_resrc + ')?(:[a-z]+)?$'
edeprel_re = re.compile(edeprel_resrc, re.U)
def validate_character_constraints(cols):
    """
    Checks general constraints on valid characters, e.g. that UPOS
    only contains [A-Z].
    """
    testlevel = 2
    if is_multiword_token(cols):
        return
    if UPOS >= len(cols):
        return # this has been already reported in trees()
    if not (re.match(r"^[A-Z]+$", cols[UPOS]) or (is_empty_node(cols) and cols[UPOS] == '_')):
        testclass = 'Morpho'
        testid = 'invalid-upos'
        testmessage = "Invalid UPOS value '%s'." % cols[UPOS]
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    if not (re.match(r"^[a-z]+(:[a-z]+)?$", cols[DEPREL]) or (is_empty_node(cols) and cols[DEPREL] == '_')):
        testclass = 'Syntax'
        testid = 'invalid-deprel'
        testmessage = "Invalid DEPREL value '%s'." % cols[DEPREL]
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    try:
        deps = deps_list(cols)
    except ValueError:
        testclass = 'Enhanced'
        testid = 'invalid-deps'
        testmessage = "Failed to parse DEPS: '%s'." % cols[DEPS]
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        return
    if any(deprel for head, deprel in deps_list(cols)
        if not edeprel_re.match(deprel)):
            testclass = 'Enhanced'
            testid = 'invalid-edeprel'
            testmessage = "Invalid enhanced relation type: '%s'." % cols[DEPS]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)

attr_val_re=re.compile('^([A-Z][A-Za-z0-9]*(?:\[[a-z0-9]+\])?)=(([A-Z0-9][A-Z0-9a-z]*)(,([A-Z0-9][A-Z0-9a-z]*))*)$',re.U)
val_re=re.compile('^[A-Z0-9][A-Za-z0-9]*',re.U)
def validate_features(cols, tag_sets, args):
    """
    Checks general constraints on feature-value format. On level 4 and higher,
    also checks that a feature-value pair is listed as approved. (Every pair
    must be allowed on level 2 because it could be defined as language-specific.
    To disallow non-universal features, test on level 4 with language 'ud'.)
    """
    global warn_on_undoc_feats
    testclass = 'Morpho'
    if FEATS >= len(cols):
        return # this has been already reported in trees()
    feats=cols[FEATS]
    if feats == '_':
        return True
    # List of permited features is language-specific.
    # The current token may be in a different language due to code switching.
    lang = args.lang
    featset = tag_sets[FEATS]
    altlang = get_alt_language(cols[MISC])
    if altlang:
        lang = altlang
        featset = get_featdata_for_language(altlang)
    feat_list=feats.split('|')
    if [f.lower() for f in feat_list]!=sorted(f.lower() for f in feat_list):
        testlevel = 2
        testid = 'unsorted-features'
        testmessage = "Morphological features must be sorted: '%s'." % feats
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    attr_set=set() # I'll gather the set of features here to check later that none is repeated.
    for f in feat_list:
        match=attr_val_re.match(f)
        if match is None:
            testlevel = 2
            testid = 'invalid-feature'
            testmessage = "Spurious morphological feature: '%s'. Should be of the form Feature=Value and must start with [A-Z] and only contain [A-Za-z0-9]." % f
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            attr_set.add(f) # to prevent misleading error "Repeated features are disallowed"
        else:
            # Check that the values are sorted as well
            attr=match.group(1)
            attr_set.add(attr)
            values=match.group(2).split(',')
            if len(values) != len(set(values)):
                testlevel = 2
                testid = 'repeated-feature-value'
                testmessage = "Repeated feature values are disallowed: '%s'" % feats
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            if [v.lower() for v in values] != sorted(v.lower() for v in values):
                testlevel = 2
                testid = 'unsorted-feature-values'
                testmessage = "If a feature has multiple values, these must be sorted: '%s'" % f
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            for v in values:
                if not val_re.match(v):
                    testlevel = 2
                    testid = 'invalid-feature-value'
                    testmessage = "Spurious value '%s' in '%s'. Must start with [A-Z0-9] and only contain [A-Za-z0-9]." % (v, f)
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                # Level 2 tests character properties and canonical order but not that the f-v pair is known.
                # Level 4 also checks whether the feature value is on the list.
                # If only universal feature-value pairs are allowed, test on level 4 with lang='ud'.
                if args.level > 3 and featset is not None:
                    testlevel = 4
                    # The featset is no longer a simple set of feature-value pairs.
                    # It is a complex database that we read from feats.json.
                    if attr not in featset:
                        testid = 'feature-unknown'
                        testmessage = "Feature %s is not documented for language [%s]." % (attr, lang)
                        if not altlang and len(warn_on_undoc_feats) > 0:
                            # If some features were excluded because they are not documented,
                            # tell the user when the first unknown feature is encountered in the data.
                            # Then erase this (long) introductory message and do not repeat it with
                            # other instances of unknown features.
                            testmessage += "\n\n" + warn_on_undoc_feats
                            warn_on_undoc_feats = ''
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                    else:
                        lfrecord = featset[attr]
                        if lfrecord['permitted']==0:
                            testid = 'feature-not-permitted'
                            testmessage = "Feature %s is not permitted in language [%s]." % (attr, lang)
                            if not altlang and len(warn_on_undoc_feats) > 0:
                                testmessage += "\n\n" + warn_on_undoc_feats
                                warn_on_undoc_feats = ''
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                        else:
                            values = lfrecord['uvalues'] + lfrecord['lvalues'] + lfrecord['unused_uvalues'] + lfrecord['unused_lvalues']
                            if not v in values:
                                testid = 'feature-value-unknown'
                                testmessage = "Value %s is not documented for feature %s in language [%s]." % (v, attr, lang)
                                if not altlang and len(warn_on_undoc_feats) > 0:
                                    testmessage += "\n\n" + warn_on_undoc_feats
                                    warn_on_undoc_feats = ''
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                            elif not cols[UPOS] in lfrecord['byupos']:
                                testid = 'feature-upos-not-permitted'
                                testmessage = "Feature %s is not permitted with UPOS %s in language [%s]." % (attr, cols[UPOS], lang)
                                if not altlang and len(warn_on_undoc_feats) > 0:
                                    testmessage += "\n\n" + warn_on_undoc_feats
                                    warn_on_undoc_feats = ''
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                            elif not v in lfrecord['byupos'][cols[UPOS]] or lfrecord['byupos'][cols[UPOS]][v]==0:
                                testid = 'feature-value-upos-not-permitted'
                                testmessage = "Value %s of feature %s is not permitted with UPOS %s in language [%s]." % (v, attr, cols[UPOS], lang)
                                if not altlang and len(warn_on_undoc_feats) > 0:
                                    testmessage += "\n\n" + warn_on_undoc_feats
                                    warn_on_undoc_feats = ''
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    if len(attr_set) != len(feat_list):
        testlevel = 2
        testid = 'repeated-feature'
        testmessage = "Repeated features are disallowed: '%s'." % feats
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)

def validate_upos(cols, tag_sets):
    if UPOS >= len(cols):
        return # this has been already reported in trees()
    if is_empty_node(cols) and cols[UPOS] == '_':
        return
    if tag_sets[UPOS] is not None and cols[UPOS] not in tag_sets[UPOS]:
        testlevel = 2
        testclass = 'Morpho'
        testid = 'unknown-upos'
        testmessage = "Unknown UPOS tag: '%s'." % cols[UPOS]
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)

def validate_deprels(cols, tag_sets, args):
    global warn_on_undoc_deps
    global warn_on_undoc_edeps
    if DEPREL >= len(cols):
        return # this has been already reported in trees()
    # List of permited relations is language-specific.
    # The current token may be in a different language due to code switching.
    deprelset = tag_sets[DEPREL]
    ###!!! Unlike with features and auxiliaries, with deprels it is less clear
    ###!!! whether we actually want to switch the set of labels when the token
    ###!!! belongs to another language. If the set is changed at all, then it
    ###!!! should be a union of the main language and the token language.
    ###!!! Otherwise we risk that, e.g., we have allowed 'flat:name' for our
    ###!!! language, the maintainers of the other language have not allowed it,
    ###!!! and then we could not use it when the foreign language is active.
    ###!!! (This has actually happened in French GSD.)
    altlang = None
    #altlang = get_alt_language(cols[MISC])
    #if altlang:
    #    deprelset = get_depreldata_for_language(altlang)
    # Test only the universal part if testing at universal level.
    deprel = cols[DEPREL]
    testlevel = 4
    if args.level < 4:
        deprel = lspec2ud(deprel)
        testlevel = 2
    if deprelset is not None and deprel not in deprelset:
        testclass = 'Syntax'
        testid = 'unknown-deprel'
        # If some relations were excluded because they are not documented,
        # tell the user when the first unknown relation is encountered in the data.
        # Then erase this (long) introductory message and do not repeat it with
        # other instances of unknown relations.
        testmessage = "Unknown DEPREL label: '%s'" % cols[DEPREL]
        if not altlang and len(warn_on_undoc_deps) > 0:
            testmessage += "\n\n" + warn_on_undoc_deps
            warn_on_undoc_deps = ''
        warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    if DEPS >= len(cols):
        return # this has been already reported in trees()
    if tag_sets[DEPS] is not None and cols[DEPS] != '_':
        for head_deprel in cols[DEPS].split('|'):
            try:
                head,deprel=head_deprel.split(':', 1)
            except ValueError:
                testclass = 'Enhanced'
                testid = 'invalid-head-deprel' # but it would have probably triggered another error above
                testmessage = "Malformed head:deprel pair '%s'." % head_deprel
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                continue
            if args.level < 4:
                deprel = lspec2ud(deprel)
            if deprel not in tag_sets[DEPS]:
                testclass = 'Enhanced'
                testid = 'unknown-edeprel'
                testmessage = "Unknown enhanced relation type '%s' in '%s'" % (deprel, head_deprel)
                if not altlang and len(warn_on_undoc_edeps) > 0:
                    testmessage += "\n\n" + warn_on_undoc_edeps
                    warn_on_undoc_edeps = ''
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)

##### Tests applicable to the whole sentence

def subset_to_words_and_empty_nodes(tree):
    """
    Only picks word and empty node lines, skips multiword token lines.
    """
    return [cols for cols in tree if is_word(cols) or is_empty_node(cols)]

def deps_list(cols):
    if DEPS >= len(cols):
        return # this has been already reported in trees()
    if cols[DEPS] == '_':
        deps = []
    else:
        deps = [hd.split(':',1) for hd in cols[DEPS].split('|')]
    if any(hd for hd in deps if len(hd) != 2):
        raise ValueError('malformed DEPS: %s' % cols[DEPS])
    return deps

basic_head_re = re.compile('^(0|[1-9][0-9]*)$', re.U)
enhanced_head_re = re.compile('^(0|[1-9][0-9]*)(\.[1-9][0-9]*)?$', re.U)
def validate_ID_references(tree):
    """
    Validates that HEAD and DEPS reference existing IDs.
    """
    testlevel = 2
    word_tree = subset_to_words_and_empty_nodes(tree)
    ids = set([cols[ID] for cols in word_tree])
    for cols in word_tree:
        if HEAD >= len(cols):
            return # this has been already reported in trees()
        # Test the basic HEAD only for non-empty nodes.
        # We have checked elsewhere that it is empty for empty nodes.
        if not is_empty_node(cols):
            match = basic_head_re.match(cols[HEAD])
            if match is None:
                testclass = 'Format'
                testid = 'invalid-head'
                testmessage = "Invalid HEAD: '%s'." % cols[HEAD]
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            if not (cols[HEAD] in ids or cols[HEAD] == '0'):
                testclass = 'Syntax'
                testid = 'unknown-head'
                testmessage = "Undefined HEAD (no such ID): '%s'." % cols[HEAD]
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        if DEPS >= len(cols):
            return # this has been already reported in trees()
        try:
            deps = deps_list(cols)
        except ValueError:
            # Similar errors have probably been reported earlier.
            testclass = 'Format'
            testid = 'invalid-deps'
            testmessage = "Failed to parse DEPS: '%s'." % cols[DEPS]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            continue
        for head, deprel in deps:
            match = enhanced_head_re.match(head)
            if match is None:
                testclass = 'Format'
                testid = 'invalid-ehead'
                testmessage = "Invalid enhanced head reference: '%s'." % head
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            if not (head in ids or head == '0'):
                testclass = 'Enhanced'
                testid = 'unknown-ehead'
                testmessage = "Undefined enhanced head reference (no such ID): '%s'." % head
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)

def validate_root(tree):
    """
    Checks that DEPREL is "root" iff HEAD is 0.
    """
    testlevel = 2
    for cols in tree:
        if is_word(cols):
            if HEAD >= len(cols):
                continue # this has been already reported in trees()
            if cols[HEAD] == '0' and lspec2ud(cols[DEPREL]) != 'root':
                testclass = 'Syntax'
                testid = '0-is-not-root'
                testmessage = "DEPREL must be 'root' if HEAD is 0."
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
            if cols[HEAD] != '0' and lspec2ud(cols[DEPREL]) == 'root':
                testclass = 'Syntax'
                testid = 'root-is-not-0'
                testmessage = "DEPREL cannot be 'root' if HEAD is not 0."
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
        if is_word(cols) or is_empty_node(cols):
            if DEPS >= len(cols):
                continue # this has been already reported in trees()
            try:
                deps = deps_list(cols)
            except ValueError:
                # Similar errors have probably been reported earlier.
                testclass = 'Format'
                testid = 'invalid-deps'
                testmessage = "Failed to parse DEPS: '%s'." % cols[DEPS]
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                continue
            for head, deprel in deps:
                if head == '0' and lspec2ud(deprel) != 'root':
                    testclass = 'Enhanced'
                    testid = 'enhanced-0-is-not-root'
                    testmessage = "Enhanced relation type must be 'root' if head is 0."
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid)
                if head != '0' and lspec2ud(deprel) == 'root':
                    testclass = 'Enhanced'
                    testid = 'enhanced-root-is-not-0'
                    testmessage = "Enhanced relation type cannot be 'root' if head is not 0."
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid)

def validate_deps(tree):
    """
    Validates that DEPS is correctly formatted and that there are no
    self-loops in DEPS.
    """
    global line_of_first_enhancement
    testlevel = 2
    node_line = sentence_line - 1
    for cols in tree:
        node_line += 1
        if not (is_word(cols) or is_empty_node(cols)):
            continue
        if DEPS >= len(cols):
            continue # this has been already reported in trees()
        # Remember whether there is at least one difference between the basic
        # tree and the enhanced graph in the entire dataset.
        if cols[DEPS] != '_' and cols[DEPS] != cols[HEAD]+':'+cols[DEPREL]:
            line_of_first_enhancement = node_line
        try:
            deps = deps_list(cols)
            heads = [float(h) for h, d in deps]
        except ValueError:
            # Similar errors have probably been reported earlier.
            testclass = 'Format'
            testid = 'invalid-deps'
            testmessage = "Failed to parse DEPS: '%s'." % cols[DEPS]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=node_line)
            return
        if heads != sorted(heads):
            testclass = 'Format'
            testid = 'unsorted-deps'
            testmessage = "DEPS not sorted by head index: '%s'" % cols[DEPS]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=node_line)
        else:
            lasth = None
            lastd = None
            for h, d in deps:
                if h == lasth:
                    if d < lastd:
                        testclass = 'Format'
                        testid = 'unsorted-deps-2'
                        testmessage = "DEPS pointing to head '%s' not sorted by relation type: '%s'" % (h, cols[DEPS])
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=node_line)
                    elif d == lastd:
                        testclass = 'Format'
                        testid = 'repeated-deps'
                        testmessage = "DEPS contain multiple instances of the same relation '%s:%s'" % (h, d)
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=node_line)
                lasth = h
                lastd = d
                ###!!! This is now also tested above in validate_root(). We must reorganize testing of the enhanced structure so that the same thing is not tested multiple times.
                # Like in the basic representation, head 0 implies relation root and vice versa.
                # Note that the enhanced graph may have multiple roots (coordination of predicates).
                #ud = lspec2ud(d)
                #if h == '0' and ud != 'root':
                #    warn("Illegal relation '%s:%s' in DEPS: must be 'root' if head is 0" % (h, d), 'Format', nodelineno=node_line)
                #if ud == 'root' and h != '0':
                #    warn("Illegal relation '%s:%s' in DEPS: cannot be 'root' if head is not 0" % (h, d), 'Format', nodelineno=node_line)
        try:
            id_ = float(cols[ID])
        except ValueError:
            # This error has been reported previously.
            return
        if id_ in heads:
            testclass = 'Enhanced'
            testid = 'deps-self-loop'
            testmessage = "Self-loop in DEPS for '%s'" % cols[ID]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=node_line)

def validate_misc(tree):
    """
    In general, the MISC column can contain almost anything. However, if there
    is a vertical bar character, it is interpreted as the separator of two
    MISC attributes, which may or may not have the form of attribute=value pair.
    In general it is not forbidden that the same attribute appears several times
    with different values, but this should not happen for selected attributes
    that are described in the UD documentation.
    """
    testlevel = 2
    testclass = 'Format'
    node_line = sentence_line - 1
    for cols in tree:
        node_line += 1
        if not (is_word(cols) or is_empty_node(cols)):
            continue
        if MISC >= len(cols):
            continue # this has been already reported in trees()
        if cols[MISC] == '_':
            continue
        misc = [ma.split('=', 1) for ma in cols[MISC].split('|')]
        mamap = {}
        for ma in misc:
            if re.match(r"^(SpaceAfter|Lang|Translit|LTranslit|Gloss|LId|LDeriv)$", ma[0]):
                mamap.setdefault(ma[0], 0)
                mamap[ma[0]] = mamap[ma[0]] + 1
        for a in list(mamap):
            if mamap[a] > 1:
                testid = 'repeated-misc'
                testmessage = "MISC attribute '%s' not supposed to occur twice" % a
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=node_line)

def build_tree(sentence):
    """
    Takes the list of non-comment lines (line = list of columns) describing
    a sentence. Returns a dictionary with items providing easier access to the
    tree structure. In case of fatal problems (missing HEAD etc.) returns None
    but does not report the error (presumably it has already been reported).

    tree ... dictionary:
      nodes ... array of word lines, i.e., lists of columns;
          mwt and empty nodes are skipped, indices equal to ids (nodes[0] is empty)
      children ... array of sets of children indices (numbers, not strings);
          indices to this array equal to ids (children[0] are the children of the root)
      linenos ... array of line numbers in the file, corresponding to nodes
          (needed in error messages)
    """
    testlevel = 2
    testclass = 'Syntax'
    global sentence_line # the line of the first token/word of the current tree (skipping comments!)
    node_line = sentence_line - 1
    children = {} # node -> set of children
    tree = {
        'nodes':    [['0', '_', '_', '_', '_', '_', '_', '_', '_', '_']], # add artificial node 0
        'children': [],
        'linenos':  [sentence_line] # for node 0
    }
    for cols in sentence:
        node_line += 1
        if not is_word(cols):
            continue
        # Even MISC may be needed when checking the annotation guidelines
        # (for instance, SpaceAfter=No must not occur inside a goeswith span).
        if MISC >= len(cols):
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return None
        try:
            id_ = int(cols[ID])
        except ValueError:
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return None
        try:
            head = int(cols[HEAD])
        except ValueError:
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return None
        if head == id_:
            testid = 'head-self-loop'
            testmessage = 'HEAD == ID for %s' % cols[ID]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=node_line)
            return None
        tree['nodes'].append(cols)
        tree['linenos'].append(node_line)
        # Incrementally build the set of children of every node.
        children.setdefault(cols[HEAD], set()).add(id_)
    for cols in tree['nodes']:
        tree['children'].append(sorted(children.get(cols[ID], [])))
    # Check that there is just one node with the root relation.
    if len(tree['children'][0]) > 1 and args.single_root:
        testid = 'multiple-roots'
        testmessage = "Multiple root words: %s" % tree['children'][0]
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
        return None
    # Return None if there are any cycles. Avoid surprises when working with the graph.
    # Presence of cycles is equivalent to presence of unreachable nodes.
    projection = set()
    get_projection(0, tree, projection)
    unreachable = set(range(1, len(tree['nodes']) - 1)) - projection
    if unreachable:
        testid = 'non-tree'
        testmessage = 'Non-tree structure. Words %s are not reachable from the root 0.' % (','.join(str(w) for w in sorted(unreachable)))
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
        return None
    return tree

def get_projection(id, tree, projection):
    """
    Like proj() above, but works with the tree data structure. Collects node ids
    in the set called projection.
    """
    nodes = list((id,))
    while nodes:
        id = nodes.pop()
        for child in tree['children'][id]:
            if child in projection:
                continue; # skip cycles
            projection.add(child)
            nodes.append(child)
    return projection

def build_egraph(sentence):
    """
    Takes the list of non-comment lines (line = list of columns) describing
    a sentence. Returns a dictionary with items providing easier access to the
    enhanced graph structure. In case of fatal problems returns None
    but does not report the error (presumably it has already been reported).
    However, once the graph has been found and built, this function verifies
    that the graph is connected and generates an error if it is not.

    egraph ... dictionary:
      nodes ... dictionary of dictionaries, each corresponding to a word or an empty node; mwt lines are skipped
          keys equal to node ids (i.e. strings that look like integers or decimal numbers; key 0 is the artificial root node)
          value is a dictionary-record:
              cols ... array of column values from the input line corresponding to the node
              children ... set of children ids (strings)
              lineno ... line number in the file (needed in error messages)
    """
    global sentence_line # the line of the first token/word of the current tree (skipping comments!)
    global line_of_first_enhanced_graph
    global line_of_first_tree_without_enhanced_graph
    node_line = sentence_line - 1
    egraph_exists = False # enhanced deps are optional
    rootnode = {
        'cols': ['0', '_', '_', '_', '_', '_', '_', '_', '_', '_'],
        'deps': [],
        'parents': set(),
        'children': set(),
        'lineno': sentence_line
    }
    egraph = {
        '0': rootnode
    } # structure described above
    nodeids = set()
    for cols in sentence:
        node_line += 1
        if is_multiword_token(cols):
            continue
        if MISC >= len(cols):
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return None
        try:
            deps = deps_list(cols)
            heads = [h for h, d in deps]
        except ValueError:
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return None
        if is_empty_node(cols):
            egraph_exists = True
        nodeids.add(cols[ID])
        # The graph may already contain a record for the current node if one of
        # the previous nodes is its child. If it doesn't, we will create it now.
        egraph.setdefault(cols[ID], {})
        egraph[cols[ID]]['cols'] = cols
        egraph[cols[ID]]['deps'] = deps_list(cols)
        egraph[cols[ID]]['parents'] = set([h for h, d in deps])
        egraph[cols[ID]].setdefault('children', set())
        egraph[cols[ID]]['lineno'] = node_line
        # Incrementally build the set of children of every node.
        for h in heads:
            egraph_exists = True
            egraph.setdefault(h, {})
            egraph[h].setdefault('children', set()).add(cols[ID])
    # We are currently testing the existence of enhanced graphs separately for each sentence.
    # However, we should not allow that one sentence has a connected egraph and another
    # has no enhanced dependencies. Such inconsistency could come as a nasty surprise
    # to the users.
    testlevel = 2
    testclass = 'Enhanced'
    if egraph_exists:
        if not line_of_first_enhanced_graph:
            line_of_first_enhanced_graph = sentence_line
            if line_of_first_tree_without_enhanced_graph:
                testid = 'edeps-only-sometimes'
                testmessage = "Enhanced graph must be empty because we saw empty DEPS on line %s" % line_of_first_tree_without_enhanced_graph
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line)
    else:
        if not line_of_first_tree_without_enhanced_graph:
            line_of_first_tree_without_enhanced_graph = sentence_line
            if line_of_first_enhanced_graph:
                testid = 'edeps-only-sometimes'
                testmessage = "Enhanced graph cannot be empty because we saw non-empty DEPS on line %s" % line_of_first_enhanced_graph
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line)
        return None
    # Check that the graph is connected. The UD v2 guidelines do not license unconnected graphs.
    # Compute projection of every node. Beware of cycles.
    projection = set()
    get_graph_projection('0', egraph, projection)
    unreachable = nodeids - projection
    if unreachable:
        sur = sorted(unreachable)
        testid = 'unconnected-egraph'
        testmessage = "Enhanced graph is not connected. Nodes %s are not reachable from any root" % sur
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
        return None
    return egraph

def get_graph_projection(id, graph, projection):
    """
    Like get_projection() above, but works with the enhanced graph data structure.
    Collects node ids in the set called projection.
    """
    nodes = list((id,))
    while nodes:
        id = nodes.pop()
        for child in graph[id]['children']:
            if child in projection:
                continue; # skip cycles
            projection.add(child)
            nodes.append(child)
    return projection



#==============================================================================
# Level 3 tests. Annotation content vs. the guidelines (only universal tests).
#==============================================================================

def validate_upos_vs_deprel(id, tree):
    """
    For certain relations checks that the dependent word belongs to an expected
    part-of-speech category. Occasionally we may have to check the children of
    the node, too.
    """
    testlevel = 3
    testclass = 'Syntax'
    cols = tree['nodes'][id]
    # This is a level 3 test, we will check only the universal part of the relation.
    deprel = lspec2ud(cols[DEPREL])
    childrels = set([lspec2ud(tree['nodes'][x][DEPREL]) for x in tree['children'][id]])
    # Certain relations are reserved for nominals and cannot be used for verbs.
    # Nevertheless, they can appear with adjectives or adpositions if they are promoted due to ellipsis.
    # Unfortunately, we cannot enforce this test because a word can be cited
    # rather than used, and then it can take a nominal function even if it is
    # a verb, as in this Upper Sorbian sentence where infinitives are appositions:
    # [hsb] Z werba danci "rejować" móže substantiw nastać danco "reja", adjektiw danca "rejowanski" a adwerb dance "rejowansce", ale tež z substantiwa martelo "hamor" móže nastać werb marteli "klepać z hamorom", adjektiw martela "hamorowy" a adwerb martele "z hamorom".
    #if re.match(r"^(nsubj|obj|iobj|obl|vocative|expl|dislocated|nmod|appos)", deprel) and re.match(r"^(VERB|AUX|ADV|SCONJ|CCONJ)", cols[UPOS]):
    #    warn("Node %s: '%s' should be a nominal but it is '%s'" % (cols[ID], deprel, cols[UPOS]), 'Syntax', lineno=False)
    # Determiner can alternate with a pronoun.
    if deprel == 'det' and not re.match(r"^(DET|PRON)", cols[UPOS]) and not 'fixed' in childrels:
        testid = 'rel-upos-det'
        testmessage = "'det' should be 'DET' or 'PRON' but it is '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    # Nummod is for "number phrases" only. This could be interpreted as NUM only,
    # but some languages treat some cardinal numbers as NOUNs, and in
    # https://github.com/UniversalDependencies/docs/issues/596,
    # we concluded that the validator will tolerate them.
    if deprel == 'nummod' and not re.match(r"^(NUM|NOUN|SYM)$", cols[UPOS]):
        testid = 'rel-upos-nummod'
        testmessage = "'nummod' should be 'NUM' but it is '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    # Advmod is for adverbs, perhaps particles but not for prepositional phrases or clauses.
    # Nevertheless, we should allow adjectives because they can be used as adverbs in some languages.
    # https://github.com/UniversalDependencies/docs/issues/617#issuecomment-488261396
    # Bohdan reports that some DET can modify adjectives in a way similar to ADV.
    # I am not sure whether advmod is the best relation for them but the alternative det is not much better, so maybe we should not enforce it. Adding DET to the tolerated UPOS tags.
    if deprel == 'advmod' and not re.match(r"^(ADV|ADJ|CCONJ|DET|PART|SYM)", cols[UPOS]) and not 'fixed' in childrels and not 'goeswith' in childrels:
        testid = 'rel-upos-advmod'
        testmessage = "'advmod' should be 'ADV' but it is '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    # Known expletives are pronouns. Determiners and particles are probably acceptable, too.
    if deprel == 'expl' and not re.match(r"^(PRON|DET|PART)$", cols[UPOS]):
        testid = 'rel-upos-expl'
        testmessage = "'expl' should normally be 'PRON' but it is '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    # Auxiliary verb/particle must be AUX.
    if deprel == 'aux' and not re.match(r"^(AUX)", cols[UPOS]):
        testid = 'rel-upos-aux'
        testmessage = "'aux' should be 'AUX' but it is '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    # Copula is an auxiliary verb/particle (AUX) or a pronoun (PRON|DET).
    if deprel == 'cop' and not re.match(r"^(AUX|PRON|DET|SYM)", cols[UPOS]):
        testid = 'rel-upos-cop'
        testmessage = "'cop' should be 'AUX' or 'PRON'/'DET' but it is '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    # Case is normally an adposition, maybe particle.
    # However, there are also secondary adpositions and they may have the original POS tag:
    # NOUN: [cs] pomocí, prostřednictvím
    # VERB: [en] including
    # Interjection can also act as case marker for vocative, as in Sanskrit: भोः भगवन् / bhoḥ bhagavan / oh sir.
    if deprel == 'case' and re.match(r"^(PROPN|ADJ|PRON|DET|NUM|AUX)", cols[UPOS]) and not 'fixed' in childrels:
        testid = 'rel-upos-case'
        testmessage = "'case' should not be '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    # Mark is normally a conjunction or adposition, maybe particle but definitely not a pronoun.
    if deprel == 'mark' and re.match(r"^(NOUN|PROPN|ADJ|PRON|DET|NUM|VERB|AUX|INTJ)", cols[UPOS]) and not 'fixed' in childrels:
        testid = 'rel-upos-mark'
        testmessage = "'mark' should not be '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    # Cc is a conjunction, possibly an adverb or particle.
    if deprel == 'cc' and re.match(r"^(NOUN|PROPN|ADJ|PRON|DET|NUM|VERB|AUX|INTJ)", cols[UPOS]) and not 'fixed' in childrels:
        testid = 'rel-upos-cc'
        testmessage = "'cc' should not be '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    if cols[DEPREL] == 'punct' and cols[UPOS] != 'PUNCT':
        testid = 'rel-upos-punct'
        testmessage = "'punct' must be 'PUNCT' but it is '%s'" % (cols[UPOS])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
    if cols[UPOS] == 'PUNCT' and not re.match(r"^(punct|root)", deprel):
        testid = 'upos-rel-punct'
        testmessage = "'PUNCT' must be 'punct' but it is '%s'" % (cols[DEPREL])
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])

def validate_left_to_right_relations(id, tree):
    """
    Certain UD relations must always go left-to-right.
    Here we currently check the rule for the basic dependencies.
    The same should also be tested for the enhanced dependencies!
    """
    testlevel = 3
    testclass = 'Syntax'
    cols = tree['nodes'][id]
    if is_multiword_token(cols):
        return
    if DEPREL >= len(cols):
        return # this has been already reported in trees()
    # According to the v2 guidelines, apposition should also be left-headed, although the definition of apposition may need to be improved.
    if re.match(r"^(conj|fixed|flat|goeswith|appos)", cols[DEPREL]):
        ichild = int(cols[ID])
        iparent = int(cols[HEAD])
        if ichild < iparent:
            # We must recognize the relation type in the test id so we can manage exceptions for legacy treebanks.
            # For conj, flat, and fixed the requirement was introduced already before UD 2.2, and all treebanks in UD 2.3 passed it.
            # For appos and goeswith the requirement was introduced before UD 2.4 and legacy treebanks are allowed to fail it.
            testid = "right-to-left-%s" % lspec2ud(cols[DEPREL])
            testmessage = "Relation '%s' must go left-to-right." % cols[DEPREL]
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])

def validate_single_subject(id, tree):
    """
    No predicate should have more than one subject.
    An xcomp dependent normally has no subject, but in some languages the
    requirement may be weaker: it could have an overt subject if it is
    correferential with a particular argument of the matrix verb. Hence we do
    not check zero subjects of xcomp dependents at present.
    Furthermore, in some situations we must allow two subjects (but not three or more).
    If a clause acts as a nonverbal predicate of another clause, and if there is
    no copula, then we must attach two subjects to the predicate of the inner
    clause: one is the predicate of the inner clause, the other is the predicate
    of the outer clause. This could in theory be recursive but in practice it isn't.
    See also issue 34 (https://github.com/UniversalDependencies/tools/issues/34).
    """
    subjects = sorted([x for x in tree['children'][id] if re.search(r"subj", lspec2ud(tree['nodes'][x][DEPREL]))])
    if len(subjects) > 2:
        # We test for more than 2, but in the error message we still say more than 1, so that we do not have to explain the exceptions.
        testlevel = 3
        testclass = 'Syntax'
        testid = 'too-many-subjects'
        testmessage = "Node has more than one subject: %s" % str(subjects)
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])

def validate_orphan(id, tree):
    """
    The orphan relation is used to attach an unpromoted orphan to the promoted
    orphan in gapping constructions. A common error is that the promoted orphan
    gets the orphan relation too. The parent of orphan is typically attached
    via a conj relation, although some other relations are plausible too.
    """
    # This is a level 3 test, we will check only the universal part of the relation.
    deprel = lspec2ud(tree['nodes'][id][DEPREL])
    if deprel == 'orphan':
        pid = int(tree['nodes'][id][HEAD])
        pdeprel = lspec2ud(tree['nodes'][pid][DEPREL])
        # We include advcl because gapping (or something very similar) can also
        # occur in subordinate clauses: "He buys companies like my mother [does] vegetables."
        # In theory, a similar pattern could also occur with reparandum.
        # A similar pattern also occurs with acl, e.g. in Latvian:
        # viņš ēd tos ābolus, ko pirms tam [ēda] tārpi ('he eats the same apples, which where [eaten] by worms before that')
        # Other clausal heads (ccomp, csubj) may be eligible as well, e.g. in Latvian
        # (see also issue 635 19.9.2019):
        # atjēdzos, ka bez angļu valodas nekur [netikšu] '[I] realised, that [I will get] nowhere without English'
        if not re.match(r"^(conj|parataxis|root|csubj|ccomp|advcl|acl|reparandum)$", pdeprel):
            testlevel = 3
            testclass = 'Syntax'
            testid = 'orphan-parent'
            testmessage = "The parent of 'orphan' should normally be 'conj' but it is '%s'." % (pdeprel)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])

def validate_functional_leaves(id, tree):
    """
    Most of the time, function-word nodes should be leaves. This function
    checks for known exceptions and warns in the other cases.
    """
    testlevel = 3
    testclass = 'Syntax'
    # This is a level 3 test, we will check only the universal part of the relation.
    deprel = lspec2ud(tree['nodes'][id][DEPREL])
    if re.match(r"^(case|mark|cc|aux|cop|det|fixed|goeswith|punct)$", deprel):
        idparent = id
        for idchild in tree['children'][id]:
            # This is a level 3 test, we will check only the universal part of the relation.
            pdeprel = lspec2ud(tree['nodes'][idparent][DEPREL])
            ###!!! We should also check that 'det' does not have children except for a limited set of exceptions!
            ###!!! (see https://universaldependencies.org/u/overview/syntax.html#function-word-modifiers)
            cdeprel = lspec2ud(tree['nodes'][idchild][DEPREL])
            # The guidelines explicitly say that negation can modify any function word
            # (see https://universaldependencies.org/u/overview/syntax.html#function-word-modifiers).
            # We cannot recognize negation simply by deprel; we have to look at the
            # part-of-speech tag and the Polarity feature as well.
            cupos = tree['nodes'][idchild][UPOS]
            cfeats = tree['nodes'][idchild][FEATS].split('|')
            if pdeprel != 'punct' and cdeprel == 'advmod' and re.match(r"^(PART|ADV)$", cupos) and 'Polarity=Neg' in cfeats:
                continue
            # Punctuation should not depend on function words if it can be projectively
            # attached to a content word. But sometimes it cannot. Czech example:
            # "Budou - li však zbývat , ukončíme" (lit. "will - if however remain , we-stop")
            # "však" depends on "ukončíme" while "budou" and "li" depend nonprojectively
            # on "zbývat" (which depends on "ukončíme"). "Budou" is aux and "li" is mark.
            # Yet the hyphen must depend on one of them because any other attachment would
            # be non-projective. Here we assume that if the parent of a punctuation node
            # is attached nonprojectively, punctuation can be attached to it to avoid its
            # own nonprojectivity.
            gap = get_gap(idparent, tree)
            if gap and cdeprel == 'punct':
                continue
            # Auxiliaries, conjunctions and case markers will tollerate a few special
            # types of modifiers.
            # Punctuation should normally not depend on a functional node. However,
            # it is possible that a functional node such as auxiliary verb is in
            # quotation marks or brackets ("must") and then these symbols should depend
            # on the functional node. We temporarily allow punctuation here, until we
            # can detect precisely the bracket situation and disallow the rest.
            # According to the guidelines
            # (https://universaldependencies.org/u/overview/syntax.html#function-word-modifiers),
            # mark can have a limited set of adverbial/oblique dependents, while the same
            # is not allowed for nodes attached as case. Nevertheless, there are valid
            # objections against this (see https://github.com/UniversalDependencies/docs/issues/618)
            # and we may want to revisit the guideline in UD v3. For the time being,
            # we make the validator more benevolent to 'case' too. (If we now force people
            # to attach adverbials higher, information will be lost and later reversal
            # of the step will not be possible.)
            # Coordinating conjunctions usually depend on a non-first conjunct, i.e.,
            # on a node whose deprel is 'conj'. However, there are paired conjunctions
            # such as "both-and", "either-or". Here the first part is attached to the
            # first conjunct. Since some function nodes (mark, case, aux, cop) can be
            # coordinated, we must allow 'cc' children under these nodes, too. However,
            # we do not want to allow 'cc' under another 'cc'. (Still, 'cc' can have
            # a 'conj' dependent. In "and/or", "or" will depend on "and" as 'conj'.)
            if re.match(r"^(mark|case)$", pdeprel) and not re.match(r"^(advmod|obl|goeswith|fixed|reparandum|conj|cc|punct)$", cdeprel):
                testid = 'leaf-mark-case'
                testmessage = "'%s' not expected to have children (%s:%s:%s --> %s:%s:%s)" % (pdeprel, idparent, tree['nodes'][idparent][FORM], pdeprel, idchild, tree['nodes'][idchild][FORM], cdeprel)
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][idchild])
            ###!!! The pdeprel regex in the following test should probably include "det".
            ###!!! I forgot to add it well in advance of release 2.4, so I am leaving it
            ###!!! out for now, so that people don't have to deal with additional load
            ###!!! of errors.
            if re.match(r"^(aux|cop)$", pdeprel) and not re.match(r"^(goeswith|fixed|reparandum|conj|cc|punct)$", cdeprel):
                testid = 'leaf-aux-cop'
                testmessage = "'%s' not expected to have children (%s:%s:%s --> %s:%s:%s)" % (pdeprel, idparent, tree['nodes'][idparent][FORM], pdeprel, idchild, tree['nodes'][idchild][FORM], cdeprel)
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][idchild])
            if re.match(r"^(cc)$", pdeprel) and not re.match(r"^(goeswith|fixed|reparandum|conj|punct)$", cdeprel):
                testid = 'leaf-cc'
                testmessage = "'%s' not expected to have children (%s:%s:%s --> %s:%s:%s)" % (pdeprel, idparent, tree['nodes'][idparent][FORM], pdeprel, idchild, tree['nodes'][idchild][FORM], cdeprel)
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][idchild])
            # Fixed expressions should not be nested, i.e., no chains of fixed relations.
            # As they are supposed to represent functional elements, they should not have
            # other dependents either, with the possible exception of conj.
            ###!!! We also allow a punct child, at least temporarily, because of fixed
            ###!!! expressions that have a hyphen in the middle (e.g. Russian "вперед-назад").
            ###!!! It would be better to keep these expressions as one token. But sometimes
            ###!!! the tokenizer is out of control of the UD data providers and it is not
            ###!!! practical to retokenize.
            elif pdeprel == 'fixed' and not re.match(r"^(goeswith|reparandum|conj|punct)$", cdeprel):
                testid = 'leaf-fixed'
                testmessage = "'%s' not expected to have children (%s:%s:%s --> %s:%s:%s)" % (pdeprel, idparent, tree['nodes'][idparent][FORM], pdeprel, idchild, tree['nodes'][idchild][FORM], cdeprel)
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][idchild])
            # Goeswith cannot have any children, not even another goeswith.
            elif pdeprel == 'goeswith':
                testid = 'leaf-goeswith'
                testmessage = "'%s' not expected to have children (%s:%s:%s --> %s:%s:%s)" % (pdeprel, idparent, tree['nodes'][idparent][FORM], pdeprel, idchild, tree['nodes'][idchild][FORM], cdeprel)
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][idchild])
            # Punctuation can exceptionally have other punct children if an exclamation
            # mark is in brackets or quotes. It cannot have other children.
            elif pdeprel == 'punct' and cdeprel != 'punct':
                testid = 'leaf-punct'
                testmessage = "'%s' not expected to have children (%s:%s:%s --> %s:%s:%s)" % (pdeprel, idparent, tree['nodes'][idparent][FORM], pdeprel, idchild, tree['nodes'][idchild][FORM], cdeprel)
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][idchild])

def collect_ancestors(id, tree, ancestors):
    """
    Usage: ancestors = collect_ancestors(nodeid, nodes, [])
    """
    pid = int(tree['nodes'][int(id)][HEAD])
    if pid == 0:
        ancestors.append(0)
        return ancestors
    if pid in ancestors:
        # Cycle has been reported on level 2. But we must jump out of it now.
        return ancestors
    ancestors.append(pid)
    return collect_ancestors(pid, tree, ancestors)

def get_caused_nonprojectivities(id, tree):
    """
    Checks whether a node is in a gap of a nonprojective edge. Report true only
    if the node's parent is not in the same gap. (We use this function to check
    that a punctuation node does not cause nonprojectivity. But if it has been
    dragged to the gap with a larger subtree, then we do not blame it.)

    tree ... dictionary:
      nodes ... array of word lines, i.e., lists of columns; mwt and empty nodes are skipped, indices equal to ids (nodes[0] is empty)
      children ... array of sets of children indices (numbers, not strings); indices to this array equal to ids (children[0] are the children of the root)
      linenos ... array of line numbers in the file, corresponding to nodes (needed in error messages)
    """
    iid = int(id) # just to be sure
    # We need to find all nodes that are not ancestors of this node and lie
    # on other side of this node than their parent. First get the set of
    # ancestors.
    ancestors = collect_ancestors(iid, tree, [])
    maxid = len(tree['nodes']) - 1
    # Get the lists of nodes to either side of id.
    # Do not look beyond the parent (if it is in the same gap, it is the parent's responsibility).
    pid = int(tree['nodes'][iid][HEAD])
    if pid < iid:
        left = range(pid + 1, iid) # ranges are open from the right (i.e. iid-1 is the last number)
        right = range(iid + 1, maxid + 1)
    else:
        left = range(1, iid)
        right = range(iid + 1, pid)
    # Exclude nodes whose parents are ancestors of id.
    sancestors = set(ancestors)
    leftna = [x for x in left if int(tree['nodes'][x][HEAD]) not in sancestors]
    rightna = [x for x in right if int(tree['nodes'][x][HEAD]) not in sancestors]
    leftcross = [x for x in leftna if int(tree['nodes'][x][HEAD]) > iid]
    rightcross = [x for x in rightna if int(tree['nodes'][x][HEAD]) < iid]
    # Once again, exclude nonprojectivities that are caused by ancestors of id.
    if pid < iid:
        rightcross = [x for x in rightcross if int(tree['nodes'][x][HEAD]) > pid]
    else:
        leftcross = [x for x in leftcross if int(tree['nodes'][x][HEAD]) < pid]
    # Do not return just a boolean value. Return the nonprojective nodes so we can report them.
    return sorted(leftcross + rightcross)

def get_gap(id, tree):
    iid = int(id) # just to be sure
    pid = int(tree['nodes'][iid][HEAD])
    if iid < pid:
        rangebetween = range(iid + 1, pid)
    else:
        rangebetween = range(pid + 1, iid)
    gap = set()
    if rangebetween:
        projection = set()
        get_projection(pid, tree, projection)
        gap = set(rangebetween) - projection
    return gap

def validate_goeswith_span(id, tree):
    """
    The relation 'goeswith' is used to connect word parts that are separated
    by whitespace and should be one word instead. We assume that the relation
    goes left-to-right, which is checked elsewhere. Here we check that the
    nodes really were separated by whitespace. If there is another node in the
    middle, it must be also attached via 'goeswith'. The parameter id refers to
    the node whose goeswith children we test.
    """
    testlevel = 3
    testclass = 'Syntax'
    gwchildren = sorted([x for x in tree['children'][id] if lspec2ud(tree['nodes'][x][DEPREL]) == 'goeswith'])
    if gwchildren:
        gwlist = sorted([id] + gwchildren)
        gwrange = list(range(id, int(tree['nodes'][gwchildren[-1]][ID]) + 1))
        # All nodes between me and my last goeswith child should be goeswith too.
        if gwlist != gwrange:
            testid = 'goeswith-gap'
            testmessage = "Violation of guidelines: gaps in goeswith group %s != %s." % (str(gwlist), str(gwrange))
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
        # Non-last node in a goeswith range must have a space after itself.
        nospaceafter = [x for x in gwlist[:-1] if 'SpaceAfter=No' in tree['nodes'][x][MISC].split('|')]
        if nospaceafter:
            testid = 'goeswith-nospace'
            testmessage = "'goeswith' cannot connect nodes that are not separated by whitespace"
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])

def validate_fixed_span(id, tree):
    """
    Like with goeswith, the fixed relation should not in general skip words that
    are not part of the fixed expression. Unlike goeswith however, there can be
    an intervening punctuation symbol.

    Update 2019-04-13: The rule that fixed expressions cannot be discontiguous
    has been challenged with examples from Swedish and Coptic, see
    https://github.com/UniversalDependencies/docs/issues/623
    For the moment, I am turning this test off. In the future, we should
    distinguish fatal errors from warnings and then this test will perhaps be
    just a warning.
    """
    return ###!!! temporarily turned off
    fxchildren = sorted([i for i in tree['children'][id] if lspec2ud(tree['nodes'][i][DEPREL]) == 'fixed'])
    if fxchildren:
        fxlist = sorted([id] + fxchildren)
        fxrange = list(range(id, int(tree['nodes'][fxchildren[-1]][ID]) + 1))
        # All nodes between me and my last fixed child should be either fixed or punct.
        fxdiff = set(fxrange) - set(fxlist)
        fxgap = [i for i in fxdiff if lspec2ud(tree['nodes'][i][DEPREL]) != 'punct']
        if fxgap:
            testlevel = 3
            testclass = 'Syntax'
            testid = 'fixed-gap'
            testmessage = "Gaps in fixed expression %s" % str(fxlist)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])

def validate_projective_punctuation(id, tree):
    """
    Punctuation is not supposed to cause nonprojectivity or to be attached
    nonprojectively.
    """
    testlevel = 3
    testclass = 'Syntax'
    # This is a level 3 test, we will check only the universal part of the relation.
    deprel = lspec2ud(tree['nodes'][id][DEPREL])
    if deprel == 'punct':
        nonprojnodes = get_caused_nonprojectivities(id, tree)
        if nonprojnodes:
            testid = 'punct-causes-nonproj'
            testmessage = "Punctuation must not cause non-projectivity of nodes %s" % nonprojnodes
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])
        gap = get_gap(id, tree)
        if gap:
            testid = 'punct-is-nonproj'
            testmessage = "Punctuation must not be attached non-projectively over nodes %s" % sorted(gap)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=tree['linenos'][id])

def validate_annotation(tree):
    """
    Checks universally valid consequences of the annotation guidelines.
    """
    for node in tree['nodes']:
        id = int(node[ID])
        validate_upos_vs_deprel(id, tree)
        validate_left_to_right_relations(id, tree)
        validate_single_subject(id, tree)
        validate_orphan(id, tree)
        validate_functional_leaves(id, tree)
        validate_fixed_span(id, tree)
        validate_goeswith_span(id, tree)
        validate_projective_punctuation(id, tree)

def validate_enhanced_annotation(graph):
    """
    Checks universally valid consequences of the annotation guidelines in the
    enhanced representation. Currently tests only phenomena specific to the
    enhanced dependencies; however, we should also test things that are
    required in the basic dependencies (such as left-to-right coordination),
    unless it is obvious that in enhanced dependencies such things are legal.
    """
    testlevel = 3
    testclass = 'Enhanced'
    # Enhanced dependencies should not contain the orphan relation.
    # However, all types of enhancements are optional and orphans are excluded
    # only if this treebank addresses gapping. We do not know it until we see
    # the first empty node.
    global line_of_first_empty_node
    global line_of_first_enhanced_orphan
    for id in graph.keys():
        if is_empty_node(graph[id]['cols']):
            if not line_of_first_empty_node:
                ###!!! This may not be exactly the first occurrence because the ids (keys) are not sorted.
                line_of_first_empty_node = graph[id]['lineno']
                # Empty node itself is not an error. Report it only for the first time
                # and only if an orphan occurred before it.
                if line_of_first_enhanced_orphan:
                    testid = 'empty-node-after-eorphan'
                    testmessage = "Empty node means that we address gapping and there should be no orphans in the enhanced graph; but we saw one on line %s" % line_of_first_enhanced_orphan
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=graph[id]['lineno'])
        udeprels = set([lspec2ud(d) for h, d in graph[id]['deps']])
        if 'orphan' in udeprels:
            if not line_of_first_enhanced_orphan:
                ###!!! This may not be exactly the first occurrence because the ids (keys) are not sorted.
                line_of_first_enhanced_orphan = graph[id]['lineno']
            # If we have seen an empty node, then the orphan is an error.
            if  line_of_first_empty_node:
                testid = 'eorphan-after-empty-node'
                testmessage = "'orphan' not allowed in enhanced graph because we saw an empty node on line %s" % line_of_first_empty_node
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=id, nodelineno=graph[id]['lineno'])



#==============================================================================
# Level 4 tests. Language-specific formal tests. Now we can check in which
# words spaces are permitted, and which Feature=Value pairs are defined.
#==============================================================================

def validate_whitespace(cols, tag_sets):
    """
    Checks a single line for disallowed whitespace.
    Here we assume that all language-independent whitespace-related tests have
    already been done in validate_cols_level1(), so we only check for words
    with spaces that are explicitly allowed in a given language.
    """
    testlevel = 4
    testclass = 'Format'
    for col_idx in (FORM,LEMMA):
        if col_idx >= len(cols):
            break # this has been already reported in trees()
        if whitespace_re.match(cols[col_idx]) is not None:
            # Whitespace found - does it pass?
            for regex in tag_sets[TOKENSWSPACE]:
                if regex.fullmatch(cols[col_idx]):
                    break
            else:
                warn_on_missing_files.add('tokens_w_space')
                testid = 'invalid-word-with-space'
                testmessage = "'%s' in column %s is not on the list of exceptions allowed to contain whitespace (data/tokens_w_space.LANG files)." % (cols[col_idx], COLNAMES[col_idx])
                warn(testmessage, testclass, testlevel=testlevel, testid=testid)



#==============================================================================
# Level 5 tests. Annotation content vs. the guidelines, language-specific.
#==============================================================================

def validate_auxiliary_verbs(cols, children, nodes, line, lang, auxlist):
    """
    Verifies that the UPOS tag AUX is used only with lemmas that are known to
    act as auxiliary verbs or particles in the given language.
    Parameters:
      'cols' ....... columns of the head node
      'children' ... list of ids
      'nodes' ...... dictionary where we can translate the node id into its
                     CoNLL-U columns
      'line' ....... line number of the node within the file
    """
    if cols[UPOS] == 'AUX' and cols[LEMMA] != '_':
        altlang = get_alt_language(cols[MISC])
        if altlang:
            lang = altlang
            auxlist, coplist = get_auxdata_for_language(altlang)
        auxdict = {}
        if auxlist != []:
            auxdict = {lang: auxlist}
        if lang == 'shopen':
            # 'desu', 'kudasai', 'yo' and 'sa' are romanized Japanese.
            lspecauxs = ['desu', 'kudasai', 'yo', 'sa']
            for ilang in auxdict:
                ilspecauxs = auxdict[ilang]
                lspecauxs = lspecauxs + ilspecauxs
        else:
            lspecauxs = auxdict.get(lang, None)
        if not lspecauxs:
            testlevel = 5
            testclass = 'Morpho'
            testid = 'aux-lemma'
            testmessage = "'%s' is not an auxiliary verb in language [%s] (there are no known approved auxiliaries in this language)" % (cols[LEMMA], lang)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=cols[ID], nodelineno=line)
        elif not cols[LEMMA] in lspecauxs:
            testlevel = 5
            testclass = 'Morpho'
            testid = 'aux-lemma'
            testmessage = "'%s' is not an auxiliary verb in language [%s]" % (cols[LEMMA], lang)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=cols[ID], nodelineno=line)

def validate_copula_lemmas(cols, children, nodes, line, lang, coplist):
    """
    Verifies that the relation cop is used only with lemmas that are known to
    act as copulas in the given language.
    Parameters:
      'cols' ....... columns of the head node
      'children' ... list of ids
      'nodes' ...... dictionary where we can translate the node id into its
                     CoNLL-U columns
      'line' ....... line number of the node within the file
    """
    if cols[DEPREL] == 'cop' and cols[LEMMA] != '_':
        altlang = get_alt_language(cols[MISC])
        if altlang:
            lang = altlang
            auxlist, coplist = get_auxdata_for_language(altlang)
        copdict = {}
        if coplist != []:
            copdict = {lang: coplist}
            # In Slavic languages, the iteratives are still variants of "to be", although they have a different lemma (derived from the main one).
            # In addition, Polish and Russian also have pronominal copulas ("to" = "this/that").
            # 'orv': ['быти', 'не быти'] See above (AUX verbs) for the comment on affirmative vs. negative lemma.
            # Lauma says that all four should be copulas despite the fact that
            # kļūt and tapt correspond to English "to become", which is not
            # copula in UD. See also the discussion in
            # https://github.com/UniversalDependencies/docs/issues/622
            # 'lv':  ['būt', 'kļūt', 'tikt', 'tapt'],
            # Two writing systems are used in Sanskrit treebanks (Devanagari and Latin) and we must list both spellings.
            # Jack: [sms] iʹlla = to not be
            # Jack says about Erzya:
            # The copula is represented by the independent copulas ульнемс (preterit) and улемс (non-past),
            # and the dependent morphology -оль (both preterit and non-past).
            # The neg арась occurs in locative/existential negation, and its
            # positive counterpart is realized in the three copulas above.
            # The neg аш in [mdf] is locative/existential negation.
            # Niko says about Komi:
            # Past tense copula is вӧвны, and in the future it is лоны, and both have a few frequentative forms.
            # 'быть' is Russian copula and it is occasionally used in spoken Komi due to code switching.
            # Komi Permyak: овлыны = to be (habitual) [Jack Rueter]
            # Sino-Tibetan languages.
            # See https://github.com/UniversalDependencies/docs/issues/653 for a discussion about Chinese copulas.
            # 是(shi4) and 为/為(wei2) should be interchangeable.
            # Sam: In Cantonese, 為 is used only in the high-standard variety, not in colloquial speech.
        if lang == 'shopen':
            # 'desu' is romanized Japanese.
            lspeccops = ['desu']
            for ilang in copdict:
                ilspeccops = copdict[ilang]
                lspeccops = lspeccops + ilspeccops
        else:
            lspeccops = copdict.get(lang, None)
        if not lspeccops:
            testlevel = 5
            testclass = 'Syntax'
            testid = 'cop-lemma'
            testmessage = "'%s' is not a copula in language [%s] (there are no known approved copulas in this language)" % (cols[LEMMA], lang)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=cols[ID], nodelineno=line)
        elif not cols[LEMMA] in lspeccops:
            testlevel = 5
            testclass = 'Syntax'
            testid = 'cop-lemma'
            testmessage = "'%s' is not a copula in language [%s]" % (cols[LEMMA], lang)
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodeid=cols[ID], nodelineno=line)

def validate_lspec_annotation(tree, lang, tag_sets):
    """
    Checks language-specific consequences of the annotation guidelines.
    """
    ###!!! Building the information about the tree is repeated and has been done in the other functions before.
    ###!!! We should remember the information and not build it several times!
    global sentence_line # the line of the first token/word of the current tree (skipping comments!)
    node_line = sentence_line - 1
    lines = {} # node id -> line number of that node (for error messages)
    nodes = {} # node id -> columns of that node
    children = {} # node -> set of children
    for cols in tree:
        node_line += 1
        if not is_word(cols):
            continue
        if HEAD >= len(cols):
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return
        if cols[HEAD]=='_':
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return
        try:
            id_ = int(cols[ID])
        except ValueError:
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return
        try:
            head = int(cols[HEAD])
        except ValueError:
            # This error has been reported on lower levels, do not report it here.
            # Do not continue to check annotation if there are elementary flaws.
            return
        # Incrementally build the set of children of every node.
        lines.setdefault(cols[ID], node_line)
        nodes.setdefault(cols[ID], cols)
        children.setdefault(cols[HEAD], set()).add(cols[ID])
    for cols in tree:
        if not is_word(cols):
            continue
        myline = lines.get(cols[ID], sentence_line)
        mychildren = children.get(cols[ID], [])
        validate_auxiliary_verbs(cols, mychildren, nodes, myline, lang, tag_sets[AUX])
        validate_copula_lemmas(cols, mychildren, nodes, myline, lang, tag_sets[COP])



#==============================================================================
# Level 6 tests for annotation of coreference and named entities. This is
# tested on demand only, as the requirements are not compulsory for UD
# releases.
#==============================================================================

global_entity_re = re.compile('^#\s*global\.Entity\s*=\s*(.+)$')
def validate_misc_entity(comments, sentence):
    """
    Optionally checks the well-formedness of the MISC attributes that pertain
    to coreference and named entities.
    """
    global comment_start_line
    global sentence_line
    global line_of_global_entity
    global global_entity_attribute_string
    global entity_attribute_number
    global entity_attribute_index
    global entity_types
    global open_entity_mentions
    global open_discontinuous_mentions
    global entity_ids_this_document
    global entity_ids_other_documents
    global entity_bridge_relations # key: srceid<tgteid pair; value: type of the entity (may be empty)
    global entity_split_antecedents # key: tgteid; value: sorted list of srceids, serialized to string
    testlevel = 6
    testclass = 'Coref'
    iline = 0
    for c in comments:
        global_entity_match = global_entity_re.match(c)
        newdoc_match = newdoc_re.match(c)
        if global_entity_match:
            # As a global declaration, global.Entity is expected only once per file.
            # However, we may be processing multiple files or people may have created
            # the file by concatening smaller files, so we will allow repeated
            # declarations iff they are identical to the first one.
            if line_of_global_entity:
                if global_entity_match.group(1) != global_entity_attribute_string:
                    testid = 'global-entity-mismatch'
                    testmessage = "New declaration of global.Entity '%s' does not match the first declaration '%s' on line %d." % (global_entity_match.group(1), global_entity_attribute_string, line_of_global_entity)
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
            else:
                line_of_global_entity = comment_start_line + iline
                global_entity_attribute_string = global_entity_match.group(1)
                if not re.match(r'^[a-z]+(-[a-z]+)*$', global_entity_attribute_string):
                    testid = 'spurious-global-entity'
                    testmessage = "Cannot parse global.Entity attribute declaration '%s'." % (global_entity_attribute_string)
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                else:
                    global_entity_attributes = global_entity_attribute_string.split('-')
                    if not 'eid' in global_entity_attributes:
                        testid = 'spurious-global-entity'
                        testmessage = "Global.Entity attribute declaration '%s' does not include 'eid'." % (global_entity_attribute_string)
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                    elif global_entity_attributes[0] != 'eid':
                        testid = 'spurious-global-entity'
                        testmessage = "Attribute 'eid' must come first in global.Entity attribute declaration '%s'." % (global_entity_attribute_string)
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                    if not 'etype' in global_entity_attributes:
                        testid = 'spurious-global-entity'
                        testmessage = "Global.Entity attribute declaration '%s' does not include 'etype'." % (global_entity_attribute_string)
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                    elif global_entity_attributes[1] != 'etype':
                        testid = 'spurious-global-entity'
                        testmessage = "Attribute 'etype' must come second in global.Entity attribute declaration '%s'." % (global_entity_attribute_string)
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                    if not 'head' in global_entity_attributes:
                        testid = 'spurious-global-entity'
                        testmessage = "Global.Entity attribute declaration '%s' does not include 'head'." % (global_entity_attribute_string)
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                    elif global_entity_attributes[2] != 'head':
                        testid = 'spurious-global-entity'
                        testmessage = "Attribute 'head' must come third in global.Entity attribute declaration '%s'." % (global_entity_attribute_string)
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                    if 'other' in global_entity_attributes and global_entity_attributes[3] != 'other':
                        testid = 'spurious-global-entity'
                        testmessage = "Attribute 'other', if present, must come fourth in global.Entity attribute declaration '%s'." % (global_entity_attribute_string)
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                    # Fill the global dictionary that maps attribute names to list indices.
                    i = 0
                    for a in global_entity_attributes:
                        if a in entity_attribute_index:
                            testid = 'spurious-global-entity'
                            testmessage = "Attribute '%s' occurs more than once in global.Entity attribute declaration '%s'." % (a, global_entity_attribute_string)
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=comment_start_line+iline)
                        else:
                            entity_attribute_index[a] = i
                        i += 1
                    entity_attribute_number = len(global_entity_attributes)
        elif newdoc_match:
            for eid in entity_ids_this_document:
                entity_ids_other_documents[eid] = entity_ids_this_document[eid]
            entity_ids_this_document = {}
        iline += 1
    iline = 0
    for cols in sentence:
        if MISC >= len(cols):
            # This error has been reported elsewhere but we cannot check MISC now.
            return
        # Add the current word to all currently open mentions. We will use it in error messages.
        for m in open_entity_mentions:
            m['text'] += ' '+cols[FORM]
            m['length'] += 1
        misc = cols[MISC].split('|')
        entity = [x for x in misc if re.match(r'^Entity=', x)]
        bridge = [x for x in misc if re.match(r'^Bridge=', x)]
        splitante = [x for x in misc if re.match(r'^SplitAnte=', x)]
        if '-' in cols[ID] and (len(entity)>0 or len(bridge)>0 or len(splitante)>0):
            testid = 'entity-mwt'
            testmessage = "Entity or coreference annotation must not occur at a multiword-token line."
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
            continue
        if len(entity)>1:
            testid = 'multiple-entity-statements'
            testmessage = "There can be at most one 'Entity=' statement in MISC but we have %s." % (str(misc))
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
            continue
        if len(bridge)>1:
            testid = 'multiple-bridge-statements'
            testmessage = "There can be at most one 'Bridge=' statement in MISC but we have %s." % (str(misc))
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
            continue
        if len(splitante)>1:
            testid = 'multiple-splitante-statements'
            testmessage = "There can be at most one 'SplitAnte=' statement in MISC but we have %s." % (str(misc))
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
            continue
        if len(bridge)>0 and len(entity)==0:
            testid = 'bridge-without-entity'
            testmessage = "The 'Bridge=' statement can only occur together with 'Entity=' in MISC but we have %s." % (str(misc))
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
            continue
        if len(splitante)>0 and len(entity)==0:
            testid = 'splitante-without-entity'
            testmessage = "The 'SplitAnte=' statement can only occur together with 'Entity=' in MISC but we have %s." % (str(misc))
            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
            continue
        # There is at most one Entity (and only if it is there, there may be also one Bridge and/or one SplitAnte).
        if len(entity)>0:
            if not line_of_global_entity:
                testid = 'entity-without-global-entity'
                testmessage = "No global.Entity comment was found before the first 'Entity' in MISC."
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                continue
            match = re.match(r'^Entity=((?:\([^( )]+(?:-[^( )]+)*\)?|[^( )]+\))+)$', entity[0])
            if not match:
                testid = 'spurious-entity-statement'
                testmessage = "Cannot parse the Entity statement '%s'." % (entity[0])
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
            else:
                entity_string = match.group(1)
                # We cannot check the rest if we cannot identify the 'eid' attribute.
                if not 'eid' in entity_attribute_index:
                    continue
                # Items of entities are pairs of [012] and a string.
                # 0 ... opening bracket; 1 ... closing bracket; 2 ... both brackets
                entities = []
                while entity_string:
                    match = re.match(r'^\(([^( )]+(-[^( )]+)*)\)', entity_string)
                    if match:
                        entities.append((2, match.group(1)))
                        entity_string = re.sub(r'^\([^( )]+(-[^( )]+)*\)', '', entity_string, count=1)
                        continue
                    match = re.match(r'^\(([^( )]+(-[^( )]+)*)', entity_string)
                    if match:
                        entities.append((0, match.group(1)))
                        entity_string = re.sub(r'^\([^( )]+(-[^( )]+)*', '', entity_string, count=1)
                        continue
                    match = re.match(r'^([^( )]+)\)', entity_string)
                    if match:
                        entities.append((1, match.group(1)))
                        entity_string = re.sub(r'^[^( )]+\)', '', entity_string, count=1)
                        continue
                    # If we pre-checked the string well, we should never arrive here!
                    warn('INTERNAL ERROR', testclass)
                # All 1 cases should precede all 0 cases.
                # The 2 cases can be either before the first 1 case, or after the last 0 case.
                seen0 = False
                seen1 = False
                seen2 = False
                # To be able to check validity of Bridge and SplitAnte, we will hash eids of mentions that start here.
                # To be able to check that no two mentions have the same span, we will hash start-end intervals for mentions that end here.
                starting_mentions = {}
                ending_mentions = {}
                for b, e in entities:
                    # First get attributes, entity id, and if applicable, part of discontinuous mention.
                    attributes = e.split('-')
                    if b==0 or b==2:
                        # Fewer attributes are allowed because trailing empty values can be omitted.
                        # More attributes are not allowed.
                        if len(attributes) > entity_attribute_number:
                            testid = 'too-many-entity-attributes'
                            testmessage = "Entity '%s' has %d attributes while only %d attributes are globally declared." % (e, len(attributes), entity_attribute_number)
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        # The raw eid (bracket eid) may include an identification of a part of a discontinuous mention,
                        # as in 'e155[1/2]'. This is fine for matching opening and closing brackets
                        # because the closing bracket must contain it too. However, to identify the
                        # cluster, we need to take the real id.
                        beid = attributes[entity_attribute_index['eid']]
                    else:
                        # No attributes other than eid are expected at the closing bracket.
                        if len(attributes) > 1:
                            testid = 'too-many-entity-attributes'
                            testmessage = "Entity '%s' has %d attributes while only eid is expected at the closing bracket." % (e, len(attributes))
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        beid = attributes[0]
                    eid = beid
                    ipart = 1
                    npart = 1
                    match = re.match(r'^(.+)\[([1-9]\d*)/([1-9]\d*)\]$', beid)
                    if match:
                        eid = match.group(1)
                        ipart = int(match.group(2))
                        npart = int(match.group(3))
                        # We should omit the square brackets if they would be [1/1].
                        if ipart == 1 and npart == 1:
                            testid = 'spurious-entity-id'
                            testmessage = "Discontinuous mention must have at least two parts but it has one in '%s'." % (beid)
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        if ipart > npart:
                            testid = 'spurious-entity-id'
                            testmessage = "Entity id '%s' of discontinuous mention says the current part is higher than total number of parts." % (beid)
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                    else:
                        if re.match(r'[\[\]]', beid):
                            testid = 'spurious-entity-id'
                            testmessage = "Entity id '%s' contains square brackets but does not have the form used in discontinuous mentions." % (beid)
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                    head = 0
                    if b==0 or b==2:
                        if ipart == 1:
                            starting_mentions[eid] = True
                        # Check all attributes of the entity.
                        if npart > 1:
                            # We want to check that values of all attributes are same in all parts (except the eid which differs in the brackets).
                            attributes_without_eid = [attributes[i] for i in range(len(attributes)) if i != entity_attribute_index['eid']]
                            attrstring_to_match = '-'.join(attributes_without_eid)
                            if eid in open_discontinuous_mentions:
                                if npart != open_discontinuous_mentions[eid]['npart']:
                                    testid = 'misplaced-mention-part'
                                    testmessage = "Unexpected part of discontinuous mention '%s': last part was '%d/%d' on line %d." % (beid, open_discontinuous_mentions[eid]['last_ipart'], open_discontinuous_mentions[eid]['npart'], open_discontinuous_mentions[eid]['line'])
                                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                                elif ipart != open_discontinuous_mentions[eid]['last_ipart']+1:
                                    testid = 'misplaced-mention-part'
                                    testmessage = "Unexpected part of discontinuous mention '%s': last part was '%d/%d' on line %d." % (beid, open_discontinuous_mentions[eid]['last_ipart'], open_discontinuous_mentions[eid]['npart'], open_discontinuous_mentions[eid]['line'])
                                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                                elif attrstring_to_match != open_discontinuous_mentions[eid]['attributes']:
                                    testid = 'mention-attribute-mismatch'
                                    testmessage = "Attribute mismatch of discontinuous mention: current part '%s', previous part '%s'." % (attrstring_to_match, open_discontinuous_mentions[eid]['attributes'])
                                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                                if ipart<npart:
                                    open_discontinuous_mentions[eid] = {'last_ipart': ipart, 'npart': npart, 'line': sentence_line+iline, 'attributes': attrstring_to_match}
                                else:
                                    open_discontinuous_mentions.pop(eid)
                            else:
                                if ipart > 1:
                                    testid = 'misplaced-mention-part'
                                    testmessage = "Unexpected part of discontinuous mention '%s': first part not encountered." % (beid)
                                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                                else:
                                    open_discontinuous_mentions[eid] = {'last_ipart': 1, 'npart': npart, 'line': sentence_line+iline, 'attributes': attrstring_to_match}
                        if eid in entity_ids_other_documents:
                            testid = 'entity-across-newdoc'
                            testmessage = "Same entity id should not occur in multiple documents; '%s' first seen on line %d, before the last newdoc." % (eid, entity_ids_other_documents[eid])
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        elif not eid in entity_ids_this_document:
                            entity_ids_this_document[eid] = sentence_line+iline
                        etype = ''
                        identity = ''
                        if 'etype' in entity_attribute_index and len(attributes) >= entity_attribute_index['etype']+1:
                            etype = attributes[entity_attribute_index['etype']]
                            # For etype values tentatively approved for CorefUD 1.0, see
                            # https://github.com/ufal/corefUD/issues/13#issuecomment-1008447464
                            if not re.match(r'^(person|place|organization|animal|plant|object|substance|time|abstract|event|other)?$', etype):
                                testid = 'spurious-entity-type'
                                testmessage = "Spurious entity type '%s'." % (etype)
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        if 'identity' in entity_attribute_index and len(attributes) >= entity_attribute_index['identity']+1:
                            identity = attributes[entity_attribute_index['identity']]
                        if 'head' in entity_attribute_index and len(attributes) >= entity_attribute_index['head']+1:
                            if not re.match(r'^[1-9][0-9]*$', attributes[entity_attribute_index['head']]):
                                testid = 'spurious-mention-head'
                                testmessage = "Entity head index '%s' must be a non-zero-starting integer." % (attributes[entity_attribute_index['head']])
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                            head = int(attributes[entity_attribute_index['head']])
                        # If this is the first mention of the entity, remember the values
                        # of the attributes that should be identical at all mentions.
                        if not eid in entity_types:
                            entity_types[eid] = (etype, identity, sentence_line+iline)
                        else:
                            # All mentions of one entity (cluster) must have the same entity type.
                            if etype != entity_types[eid][0]:
                                testid = 'entity-type-mismatch'
                                testmessage = "Entity '%s' cannot have type '%s' that does not match '%s' from the first mention on line %d." % (eid, etype, entity_types[eid][0], entity_types[eid][2])
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                            # All mentions of one entity (cluster) must have the same identity (Wikipedia link or similar).
                            if identity != entity_types[eid][1]:
                                testid = 'entity-identity-mismatch'
                                testmessage = "Entity '%s' cannot have identity '%s' that does not match '%s' from the first mention on line %d." % (eid, identity, entity_types[eid][1], entity_types[eid][2])
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                    # Now we know the beid, eid, as well as all other attributes.
                    # We can check the well-nestedness of brackets.
                    if b==0:
                        if seen2 and not seen1:
                            testid = 'spurious-entity-statement'
                            testmessage = "If there are no closing entity brackets, single-node entity must follow all opening entity brackets in '%s'." % (entity[0])
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        if seen0 and seen2:
                            testid = 'spurious-entity-statement'
                            testmessage = "Single-node entity must either precede all closing entity brackets or follow all opening entity brackets in '%s'." % (entity[0])
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        seen0 = True
                        seen2 = False
                        # Remember the line where the entity mention starts.
                        mention = {'beid': beid, 'line': sentence_line+iline, 'text': cols[FORM], 'length': 1, 'head': head}
                        open_entity_mentions.append(mention)
                    elif b==2:
                        if seen1 and not seen0:
                            testid = 'spurious-entity-statement'
                            testmessage = "If there are no opening entity brackets, single-node entity must precede all closing entity brackets in '%s'." % (entity[0])
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        seen2 = True
                        # Check that head is not outside. Head does not have to be 1 if this is a part of a discontinuous mention.
                        mention_length = 1
                        if npart > 1 and eid in open_discontinuous_mentions:
                            if 'length' in open_discontinuous_mentions[eid]:
                                open_discontinuous_mentions[eid]['length'] += mention_length
                                mention_length = open_discontinuous_mentions[eid]['length']
                            else:
                                open_discontinuous_mentions[eid]['length'] = mention_length
                        if ipart == npart and mention_length < head:
                            testid = 'mention-head-out-of-range'
                            testmessage = "Entity mention head is specified as %d in '%s' but the mention has only %d nodes." % (head, e, mention_length)
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                    else: # b==1
                        if seen0:
                            testid = 'spurious-entity-statement'
                            testmessage = "All closing entity brackets must precede all opening entity brackets in '%s'." % (entity[0])
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        seen1 = True
                        # Check only well-nestedness of brackets.
                        if len(open_entity_mentions)==0:
                            testid = 'ill-nested-entities'
                            testmessage = "Cannot close entity '%s' because there are no open entities." % (beid)
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        else:
                            if beid != open_entity_mentions[-1]['beid']:
                                testid = 'ill-nested-entities'
                                testmessage = "Entity mentions are not well nested: closing '%s' while the innermost open entity is '%s' from line %d: %s." % (eid, open_entity_mentions[-1]['beid'], open_entity_mentions[-1]['line'], str(open_entity_mentions))
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                            # To prevent the subsequent error messages from growing infinitely, try to find and close the entity whether or not it was well-nested.
                            for i in reversed(range(len(open_entity_mentions))):
                                if open_entity_mentions[i]['beid'] == beid:
                                    # Before we close the mention, check that it had enough nodes for the specified head index.
                                    # If this is a part of a discontinuous mention, update the total length of the mention.
                                    mention_length = open_entity_mentions[i]['length']
                                    if npart > 1 and eid in open_discontinuous_mentions:
                                        if 'length' in open_discontinuous_mentions[eid]:
                                            open_discontinuous_mentions[eid]['length'] += mention_length
                                            mention_length = open_discontinuous_mentions[eid]['length']
                                        else:
                                            open_discontinuous_mentions[eid]['length'] = mention_length
                                    if ipart == npart and mention_length < open_entity_mentions[i]['head']:
                                        testid = 'mention-head-out-of-range'
                                        testmessage = "Entity mention head was specified as %d on line %d but the mention has only %d nodes." % (open_entity_mentions[i]['head'], open_entity_mentions[i]['line'], mention_length)
                                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                                    # Check that no two mentions have identical spans.
                                    ###!!! Again, we will have to refine the implementation so that discontinuous mentions work, too.
                                    ending_mention_key = str(open_entity_mentions[i]['line'])
                                    if ending_mention_key in ending_mentions:
                                        testid = 'same-span-entity-mentions'
                                        testmessage = "Entity mentions '%s' and '%s' from line %d have the same span." % (ending_mentions[ending_mention_key], beid, open_entity_mentions[i]['line'])
                                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                                    else:
                                        ending_mentions[ending_mention_key] = beid
                                    open_entity_mentions.pop(i)
                                    break
            # Now we are done with checking the 'Entity=' statement.
            # If there are also 'Bridge=' or 'SplitAnte=' statements, check them too.
            if len(bridge) > 0:
                match = re.match(r'^Bridge=([^(< :>)]+<[^(< :>)]+(:[a-z]+)?(,[^(< :>)]+<[^(< :>)]+(:[a-z]+)?)*)$', bridge[0])
                if not match:
                    testid = 'spurious-bridge-statement'
                    testmessage = "Cannot parse the Bridge statement '%s'." % (bridge[0])
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                else:
                    bridges = match.group(1).split(',')
                    # Hash src<tgt pairs and make sure they are not repeated.
                    srctgt = {}
                    for b in bridges:
                        match = re.match(r'([^(< :>)]+)<([^(< :>)]+)(?::([a-z]+))?^$', b)
                        if match:
                            srceid = match.group(1)
                            tgteid = match.group(2)
                            relation = match.group(3) # optional
                            bridgekey = srceid+'<'+tgteid
                            if not tgteid in starting_mentions:
                                testid = 'misplaced-bridge-statement'
                                testmessage = "Bridge relation '%s' must be annotated at the beginning of a mention of entity '%s'." % (b, tgteid)
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                            if bridgekey in srctgt:
                                testid = 'repeated-bridge-relation'
                                testmessage = "Bridge relation '%s' must not be repeated in '%s'." % (bridgekey, b)
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                            else:
                                srctgt[bridgekey] = True
                            # Check in the global dictionary whether this relation has been specified at another mention.
                            if bridgekey in entity_bridge_relations:
                                if relation != entity_bridge_relations[bridgekey]['relation']:
                                    testid = 'bridge-relation-mismatch'
                                    testmessage = "Bridge relation '%s' type does not match '%s' specified earlier on line %d." % (b, entity_bridge_relations[bridgekey]['relation'], entity_bridge_relations[bridgekey]['line'])
                                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                            else:
                                entity_bridge_relations[bridgekey] = {'relation': relation, 'line': sentence_line+iline}
            if len(splitante) > 0:
                match = re.match(r'^SplitAnte=([^(< :>)]+<[^(< :>)]+(,[^(< :>)]+<[^(< :>)]+)*)$', splitante[0])
                if not match:
                    testid = 'spurious-splitante-statement'
                    testmessage = "Cannot parse the SplitAnte statement '%s'." % (splitante[0])
                    warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                else:
                    antecedents = match.group(1).split(',')
                    # Hash src<tgt pairs and make sure they are not repeated. Also remember the number of antecedents for each target.
                    srctgt = {}
                    tgtante = {}
                    for a in antecedents:
                        match = re.match(r'^([^(< :>)]+)<([^(< :>)]+)$', a)
                        if match:
                            srceid = match.group(1)
                            tgteid = match.group(2)
                            if not tgteid in starting_mentions:
                                testid = 'misplaced-bridge-statement'
                                testmessage = "SplitAnte relation '%s' must be annotated at the beginning of a mention of entity '%s'." % (a, tgteid)
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                            if srceid+'<'+tgteid in srctgt:
                                testid = 'repeated-splitante-relation'
                                testmessage = "SplitAnte relation '%s' must not be repeated in '%s'." % (srceid+'<'+tgteid, ','.join(antecedents))
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                            else:
                                srctgt[srceid+'<'+tgteid] = True
                            if tgteid in tgtante:
                                tgtante[tgteid].append(srceid)
                            else:
                                tgtante[tgteid] = [srceid]
                    for tgteid in tgtante:
                        if len(tgtante[tgteid]) == 1:
                            testid = 'only-one-split-antecedent'
                            testmessage = "SplitAnte statement '%s' must specify at least two antecedents for entity '%s'." % (','.join(antecedents), tgteid)
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        # Check in the global dictionary whether this relation has been specified at another mention.
                        tgtante[tgteid].sort()
                        if tgteid in entity_split_antecedents:
                            if tgtante[tgteid] != entity_split_antecedents[tgteid]['antecedents']:
                                testid = 'split-antecedent-mismatch'
                                testmessage = "Split antecedent of entity '%s' does not match '%s' specified earlier on line %d." % (tgteid, entity_split_antecedents[tgteid]['antecedents'], entity_split_antecedents[tgteid]['line'])
                                warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
                        else:
                            entity_split_antecedents[tgteid] = {'antecedents': str(tgtante[tgteid]), 'line': sentence_line+iline}
        iline += 1
    if len(open_entity_mentions)>0:
        testid = 'cross-sentence-mention'
        testmessage = "Entity mentions must not cross sentence boundaries; still open at sentence end: %s." % (str(open_entity_mentions))
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
        # Close the mentions forcibly. Otherwise one omitted closing bracket would cause the error messages to to explode because the words would be collected from the remainder of the file.
        open_entity_mentions = []
    if len(open_discontinuous_mentions)>0:
        testid = 'cross-sentence-mention'
        testmessage = "Entity mentions must not cross sentence boundaries; still open at sentence end: %s." % (str(open_discontinuous_mentions))
        warn(testmessage, testclass, testlevel=testlevel, testid=testid, nodelineno=sentence_line+iline)
        # Close the mentions forcibly. Otherwise one omission would cause the error messages to to explode because the words would be collected from the remainder of the file.
        open_discontinuous_mentions = {}



#==============================================================================
# Main part.
#==============================================================================

def validate(inp, out, args, tag_sets, known_sent_ids):
    global tree_counter
    for comments, sentence in trees(inp, tag_sets, args):
        tree_counter += 1
        #the individual lines have been validated already in trees()
        #here go tests which are done on the whole tree
        idseqok = validate_ID_sequence(sentence) # level 1
        validate_token_ranges(sentence) # level 1
        if args.level > 1:
            validate_sent_id(comments, known_sent_ids, args.lang) # level 2
            if args.check_tree_text:
                validate_text_meta(comments, sentence) # level 2
            validate_root(sentence) # level 2
            validate_ID_references(sentence) # level 2
            validate_deps(sentence) # level 2 and up
            validate_misc(sentence) # level 2 and up
            if args.check_coref:
                validate_misc_entity(comments, sentence) # optional for CorefUD treebanks
            # Avoid building tree structure if the sequence of node ids is corrupted.
            if idseqok:
                tree = build_tree(sentence) # level 2 test: tree is single-rooted, connected, cycle-free
                egraph = build_egraph(sentence) # level 2 test: egraph is connected
            else:
                tree = None
                egraph = None
            if tree:
                if args.level > 2:
                    validate_annotation(tree) # level 3
                    if args.level > 4:
                        validate_lspec_annotation(sentence, args.lang, tag_sets) # level 5
            else:
                testlevel = 2
                testclass = 'Format'
                testid = 'skipped-corrupt-tree'
                testmessage = "Skipping annotation tests because of corrupt tree structure."
                warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
            if egraph:
                if args.level > 2:
                    validate_enhanced_annotation(egraph) # level 3
    validate_newlines(inp) # level 1

def load_file(filename):
    res = set()
    with io.open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line=line.strip()
            if not line or line.startswith('#'):
                continue
            res.add(line)
    return res

def load_upos_set(filename):
    """
    Loads the list of permitted UPOS tags and returns it as a set.
    """
    res = load_file(os.path.join(THISDIR, 'data', filename))
    return res

def load_feat_set(filename_langspec, lcode):
    """
    Loads the list of permitted feature-value pairs and returns it as a set.
    """
    global featdata
    global warn_on_undoc_feats
    with open(os.path.join(THISDIR, 'data', filename_langspec), 'r', encoding='utf-8') as f:
        all_features_0 = json.load(f)
    featdata = all_features_0['features']
    featset = get_featdata_for_language(lcode)
    # Prepare a global message about permitted features and values. We will add
    # it to the first error message about an unknown feature. Note that this
    # global information pertains to the default validation language and it
    # should not be used with code-switched segments in alternative languages.
    msg = ''
    if not lcode in featdata:
        msg += "No feature-value pairs have been permitted for language [%s].\n" % (lcode)
        msg += "They can be permitted at the address below (if the language has an ISO code and is registered with UD):\n"
        msg += "https://quest.ms.mff.cuni.cz/udvalidator/cgi-bin/unidep/langspec/specify_feature.pl\n"
        warn_on_undoc_feats = msg
    else:
        # Identify feature values that are permitted in the current language.
        for f in featset:
            for e in featset[f]['errors']:
                msg += "ERROR in _%s/feat/%s.md: %s\n" % (lcode, f, e)
        res = set()
        for f in featset:
            if featset[f]['permitted'] > 0:
                for v in featset[f]['uvalues']:
                    res.add(f+'='+v)
                for v in featset[f]['lvalues']:
                    res.add(f+'='+v)
        sorted_documented_features = sorted(res)
        msg += "The following %d feature values are currently permitted in language [%s]:\n" % (len(sorted_documented_features), lcode)
        msg += ', '.join(sorted_documented_features) + "\n"
        msg += "If a language needs a feature that is not documented in the universal guidelines, the feature must\n"
        msg += "have a language-specific documentation page in a prescribed format.\n"
        msg += "See https://universaldependencies.org/contributing_language_specific.html for further guidelines.\n"
        msg += "All features including universal must be specifically turned on for each language in which they are used.\n"
        msg += "See https://quest.ms.mff.cuni.cz/udvalidator/cgi-bin/unidep/langspec/specify_feature.pl for details.\n"
        warn_on_undoc_feats = msg
    return featset

def get_featdata_for_language(lcode):
    """
    Searches the previously loaded database of feature-value combinations.
    Returns the lists for a given language code. For most CoNLL-U files,
    this function is called only once at the beginning. However, some files
    contain code-switched data and we may temporarily need to validate
    another language.
    """
    global featdata
    ###!!! If lcode is 'ud', we should permit all universal feature-value pairs,
    ###!!! regardless of language-specific documentation.
    # Do not crash if the user asks for an unknown language.
    if not lcode in featdata:
        return {} ###!!! or None?
    return featdata[lcode]

def load_deprel_set(filename_langspec, lcode):
    """
    Loads the list of permitted relation types and returns it as a set.
    """
    global depreldata
    global warn_on_undoc_deps
    with open(os.path.join(THISDIR, 'data', filename_langspec), 'r', encoding='utf-8') as f:
        all_deprels_0 = json.load(f)
    depreldata = all_deprels_0['deprels']
    deprelset = get_depreldata_for_language(lcode)
    # Prepare a global message about permitted relation labels. We will add
    # it to the first error message about an unknown relation. Note that this
    # global information pertains to the default validation language and it
    # should not be used with code-switched segments in alternative languages.
    msg = ''
    if len(deprelset) == 0:
        msg += "No dependency relation types have been permitted for language [%s].\n" % (lcode)
        msg += "They can be permitted at the address below (if the language has an ISO code and is registered with UD):\n"
        msg += "https://quest.ms.mff.cuni.cz/udvalidator/cgi-bin/unidep/langspec/specify_deprel.pl\n"
    else:
        # Identify dependency relations that are permitted in the current language.
        # If there are errors in documentation, identify the erroneous doc file.
        # Note that depreldata[lcode] may not exist even though we have a non-empty
        # set of relations, if lcode is 'ud'.
        if lcode in depreldata:
            for r in depreldata[lcode]:
                file = re.sub(r':', r'-', r)
                if file == 'aux':
                    file = 'aux_'
                for e in depreldata[lcode][r]['errors']:
                    msg += "ERROR in _%s/dep/%s.md: %s\n" % (lcode, file, e)
        sorted_documented_relations = sorted(deprelset)
        msg += "The following %d relations are currently permitted in language [%s]:\n" % (len(sorted_documented_relations), lcode)
        msg += ', '.join(sorted_documented_relations) + "\n"
        msg += "If a language needs a relation subtype that is not documented in the universal guidelines, the relation\n"
        msg += "must have a language-specific documentation page in a prescribed format.\n"
        msg += "See https://universaldependencies.org/contributing_language_specific.html for further guidelines.\n"
        msg += "Documented dependency relations can be specifically turned on/off for each language in which they are used.\n"
        msg += "See https://quest.ms.mff.cuni.cz/udvalidator/cgi-bin/unidep/langspec/specify_deprel.pl for details.\n"
        # Save the message in a global variable.
        # We will add it to the first error message about an unknown feature in the data.
    warn_on_undoc_deps = msg
    return deprelset

def get_depreldata_for_language(lcode):
    """
    Searches the previously loaded database of dependency relation labels.
    Returns the lists for a given language code. For most CoNLL-U files,
    this function is called only once at the beginning. However, some files
    contain code-switched data and we may temporarily need to validate
    another language.
    """
    global depreldata
    deprelset = set()
    # If lcode is 'ud', we should permit all universal dependency relations,
    # regardless of language-specific documentation.
    ###!!! We should be able to take them from the documentation JSON files instead of listing them here.
    if lcode == 'ud':
        deprelset = set(['nsubj', 'obj', 'iobj', 'csubj', 'ccomp', 'xcomp', 'obl', 'vocative', 'expl', 'dislocated', 'advcl', 'advmod', 'discourse', 'aux', 'cop', 'mark', 'nmod', 'appos', 'nummod', 'acl', 'amod', 'det', 'clf', 'case', 'conj', 'cc', 'fixed', 'flat', 'compound', 'list', 'parataxis', 'orphan', 'goeswith', 'reparandum', 'punct', 'root', 'dep'])
    elif lcode in depreldata:
        for r in depreldata[lcode]:
            if depreldata[lcode][r]['permitted'] > 0:
                deprelset.add(r)
    return deprelset

def load_edeprel_set(filename_langspec, lcode, basic_deprels):
    """
    Loads the list of permitted enhanced relation types (case markers) and returns it as a set.
    """
    global edepreldata
    global warn_on_undoc_edeps
    with open(os.path.join(THISDIR, 'data', filename_langspec), 'r', encoding='utf-8') as f:
        all_edeprels_0 = json.load(f)
    edepreldata = all_edeprels_0['edeprels']
    edeprelset = get_edepreldata_for_language(lcode, basic_deprels)
    # Prepare a global message about permitted relation labels. We will add
    # it to the first error message about an unknown relation. Note that this
    # global information pertains to the default validation language and it
    # should not be used with code-switched segments in alternative languages.
    msg = ''
    if len(edeprelset) == 0:
        msg += "No enhanced dependency relation types (case markers) have been permitted for language [%s].\n" % (lcode)
        msg += "They can be permitted at the address below (if the language has an ISO code and is registered with UD):\n"
        msg += "https://quest.ms.mff.cuni.cz/udvalidator/cgi-bin/unidep/langspec/specify_edeprel.pl\n"
    else:
        # Identify dependency relations that are permitted in the current language.
        # If there are errors in documentation, identify the erroneous doc file.
        # Note that depreldata[lcode] may not exist even though we have a non-empty
        # set of relations, if lcode is 'ud'.
        sorted_case_markers = sorted(edeprelset)
        msg += "The following %d enhanced relations are currently permitted in language [%s]:\n" % (len(sorted_case_markers), lcode)
        msg += ', '.join(sorted_case_markers) + "\n"
        msg += "See https://quest.ms.mff.cuni.cz/udvalidator/cgi-bin/unidep/langspec/specify_edeprel.pl for details.\n"
        # Save the message in a global variable.
        # We will add it to the first error message about an unknown feature in the data.
    warn_on_undoc_edeps = msg
    return edeprelset

def get_edepreldata_for_language(lcode, basic_deprels):
    """
    Searches the previously loaded database of enhanced case markers.
    Returns the lists for a given language code. For most CoNLL-U files,
    this function is called only once at the beginning. However, some files
    contain code-switched data and we may temporarily need to validate
    another language.
    """
    global edepreldata
    edeprelset = basic_deprels|{'ref'}
    for bdeprel in basic_deprels:
        if re.match(r'^[nc]subj(:|$)', bdeprel):
            edeprelset.add(bdeprel+':xsubj')
    if lcode in edepreldata:
        for c in edepreldata[lcode]:
            for deprel in edepreldata[lcode][c]['extends']:
                for bdeprel in basic_deprels:
                    if bdeprel == deprel or re.match(r'^'+deprel+':', bdeprel):
                        edeprelset.add(bdeprel+':'+c)
    return edeprelset

def load_set(f_name_ud, f_name_langspec, validate_langspec=False, validate_enhanced=False):
    """
    Loads a list of values from the two files, and returns their
    set. If f_name_langspec doesn't exist, loads nothing and returns
    None (ie this taglist is not checked for the given language). If f_name_langspec
    is None, only loads the UD one. This is probably only useful for CPOS which doesn't
    allow language-specific extensions. Set validate_langspec=True when loading basic dependencies.
    That way the language specific deps will be checked to be truly extensions of UD ones.
    Set validate_enhanced=True when loading enhanced dependencies. They will be checked to be
    truly extensions of universal relations, too; but a more relaxed regular expression will
    be checked because enhanced relations may contain stuff that is forbidden in the basic ones.
    """
    res = load_file(os.path.join(THISDIR, 'data', f_name_ud))
    # Now res holds UD
    # Next load and optionally check the langspec extensions
    if f_name_langspec is not None and f_name_langspec != f_name_ud:
        path_langspec = os.path.join(THISDIR,"data",f_name_langspec)
        if os.path.exists(path_langspec):
            global curr_fname
            curr_fname = path_langspec # so warn() does not fail on undefined curr_fname
            l_spec = load_file(path_langspec)
            for v in l_spec:
                if validate_enhanced:
                    # We are reading the list of language-specific dependency relations in the enhanced representation
                    # (i.e., the DEPS column, not DEPREL). Make sure that they match the regular expression that
                    # restricts enhanced dependencies.
                    if not edeprel_re.match(v):
                        testlevel = 4
                        testclass = 'Enhanced'
                        testid = 'edeprel-def-regex'
                        testmessage = "Spurious language-specific enhanced relation '%s' - it does not match the regular expression that restricts enhanced relations." % v
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
                        continue
                elif validate_langspec:
                    # We are reading the list of language-specific dependency relations in the basic representation
                    # (i.e., the DEPREL column, not DEPS). Make sure that they match the regular expression that
                    # restricts basic dependencies. (In particular, that they do not contain extensions allowed in
                    # enhanced dependencies, which should be listed in a separate file.)
                    if not re.match(r"^[a-z]+(:[a-z]+)?$", v):
                        testlevel = 4
                        testclass = 'Syntax'
                        testid = 'deprel-def-regex'
                        testmessage = "Spurious language-specific relation '%s' - in basic UD, it must match '^[a-z]+(:[a-z]+)?'." % v
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
                        continue
                if validate_langspec or validate_enhanced:
                    try:
                        parts=v.split(':')
                        if parts[0] not in res and parts[0] != 'ref':
                            testlevel = 4
                            testclass = 'Syntax'
                            testid = 'deprel-def-universal-part'
                            testmessage = "Spurious language-specific relation '%s' - not an extension of any UD relation." % v
                            warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
                            continue
                    except:
                        testlevel = 4
                        testclass = 'Syntax'
                        testid = 'deprel-def-universal-part'
                        testmessage = "Spurious language-specific relation '%s' - not an extension of any UD relation." % v
                        warn(testmessage, testclass, testlevel=testlevel, testid=testid, lineno=False)
                        continue
                res.add(v)
    return res

def get_auxdata_for_language(lcode):
    """
    Searches the previously loaded database of auxiliary/copula lemmas. Returns
    the AUX and COP lists for a given language code. For most CoNLL-U files,
    this function is called only once at the beginning. However, some files
    contain code-switched data and we may temporarily need to validate
    another language.
    """
    global auxdata
    # If any of the functions of the lemma is other than cop.PRON, it counts as an auxiliary.
    # If any of the functions of the lemma is cop.*, it counts as a copula.
    auxlist = []
    coplist = []
    if lcode == 'shopen':
        for lcode1 in auxdata.keys():
            lemmalist = auxdata[lcode1].keys()
            auxlist = auxlist + [x for x in lemmalist if len([y for y in auxdata[lcode1][x]['functions'] if y['function'] != 'cop.PRON']) > 0]
            coplist = coplist + [x for x in lemmalist if len([y for y in auxdata[lcode1][x]['functions'] if re.match("^cop\.", y['function'])]) > 0]
    else:
        lemmalist = auxdata.get(lcode, {}).keys()
        auxlist = [x for x in lemmalist if len([y for y in auxdata[lcode][x]['functions'] if y['function'] != 'cop.PRON']) > 0]
        coplist = [x for x in lemmalist if len([y for y in auxdata[lcode][x]['functions'] if re.match("^cop\.", y['function'])]) > 0]
    return auxlist, coplist

def get_alt_language(misc):
    """
    Takes the value of the MISC column for a token and checks it for the
    attribute Lang=xxx. If present, it is interpreted as the code of the
    language in which the current token is. This is uselful for code switching,
    if a phrase is in a language different from the main language of the
    document. The validator can then temporarily switch to a different set
    of language-specific tests.
    """
    misclist = misc.split('|')
    p = re.compile(r'Lang=(.+)')
    for attr in misclist:
        m = p.match(attr)
        if m:
            return m.group(1)
    return None

if __name__=="__main__":
    opt_parser = argparse.ArgumentParser(description="CoNLL-U validation script. Python 3 is needed to run it!")

    io_group = opt_parser.add_argument_group("Input / output options")
    io_group.add_argument('--quiet', dest="quiet", action="store_true", default=False, help='Do not print any error messages. Exit with 0 on pass, non-zero on fail.')
    io_group.add_argument('--max-err', action="store", type=int, default=20, help='How many errors to output before exiting? 0 for all. Default: %(default)d.')
    io_group.add_argument('input', nargs='*', help='Input file name(s), or "-" or nothing for standard input.')

    list_group = opt_parser.add_argument_group("Tag sets", "Options relevant to checking tag sets.")
    list_group.add_argument("--lang", action="store", required=True, default=None, help="Which langauge are we checking? If you specify this (as a two-letter code), the tags will be checked using the language-specific files in the data/ directory of the validator.")
    list_group.add_argument("--level", action="store", type=int, default=5, dest="level", help="Level 1: Test only CoNLL-U backbone. Level 2: UD format. Level 3: UD contents. Level 4: Language-specific labels. Level 5: Language-specific contents.")

    tree_group = opt_parser.add_argument_group("Tree constraints", "Options for checking the validity of the tree.")
    tree_group.add_argument("--multiple-roots", action="store_false", default=True, dest="single_root", help="Allow trees with several root words (single root required by default).")

    meta_group = opt_parser.add_argument_group("Metadata constraints", "Options for checking the validity of tree metadata.")
    meta_group.add_argument("--no-tree-text", action="store_false", default=True, dest="check_tree_text", help="Do not test tree text. For internal use only, this test is required and on by default.")
    meta_group.add_argument("--no-space-after", action="store_false", default=True, dest="check_space_after", help="Do not test presence of SpaceAfter=No.")

    coref_group = opt_parser.add_argument_group("Coreference / entity constraints", "Options for checking coreference and entity annotation.")
    coref_group.add_argument('--coref', action='store_true', default=False, dest='check_coref', help='Test coreference and entity-related annotation in MISC.')

    args = opt_parser.parse_args() #Parsed command-line arguments
    error_counter={} # Incremented by warn()  {key: error type value: its count}
    tree_counter=0

    # Level of validation
    if args.level < 1:
        print('Option --level must not be less than 1; changing from %d to 1' % args.level, file=sys.stderr)
        args.level = 1
    # No language-specific tests for levels 1-3
    # Anyways, any Feature=Value pair should be allowed at level 3 (because it may be language-specific),
    # and any word form or lemma can contain spaces (because language-specific guidelines may allow it).
    # We can also test language 'ud' on level 4; then it will require that no language-specific features are present.
    if args.level < 4:
        args.lang = 'ud'

    # Sets of tags for every column that needs to be checked, plus (in v2) other sets, like the allowed tokens with space
    tagsets = {XPOS:None, UPOS:None, FEATS:None, DEPREL:None, DEPS:None, TOKENSWSPACE:None, AUX:None}

    if args.lang:
        tagsets[UPOS] = load_upos_set('cpos.ud')
        tagsets[FEATS] = load_feat_set('feats.json', args.lang)
        tagsets[DEPREL] = load_deprel_set('deprels.json', args.lang)
        # All relations available in DEPREL are also allowed in DEPS.
        # In addition, there might be relations that are only allowed in DEPS.
        # One of them, "ref", is universal and we currently mention it directly
        # in the code, although there is also a file "edeprel.ud".
        #tagsets[DEPS] = tagsets[DEPREL]|{"ref"}|load_set("deprel.ud","edeprel."+args.lang,validate_enhanced=True)
        tagsets[DEPS] = load_edeprel_set('edeprels.json', args.lang, tagsets[DEPREL])
        tagsets[TOKENSWSPACE] = load_set('tokens_w_space.ud', 'tokens_w_space.'+args.lang)
        tagsets[TOKENSWSPACE] = [re.compile(regex, re.U) for regex in tagsets[TOKENSWSPACE]] #...turn into compiled regular expressions
        # Read the list of auxiliaries from the JSON file.
        # This file must not be edited directly!
        # Use the web interface at https://quest.ms.mff.cuni.cz/udvalidator/cgi-bin/unidep/langspec/specify_auxiliary.pl instead!
        with open(os.path.join(THISDIR, 'data', 'data.json'), 'r', encoding='utf-8') as f:
            jsondata = json.load(f)
        auxdata = jsondata['auxiliaries']
        tagsets[AUX], tagsets[COP] = get_auxdata_for_language(args.lang)

    out=sys.stdout # hard-coding - does this ever need to be anything else?

    try:
        known_sent_ids=set()
        open_files=[]
        if args.input==[]:
            args.input.append('-')
        for fname in args.input:
            if fname=='-':
                # Set PYTHONIOENCODING=utf-8 before starting Python. See https://docs.python.org/3/using/cmdline.html#envvar-PYTHONIOENCODING
                # Otherwise ANSI will be read in Windows and locale-dependent encoding will be used elsewhere.
                open_files.append(sys.stdin)
            else:
                open_files.append(io.open(fname, 'r', encoding='utf-8'))
        for curr_fname, inp in zip(args.input, open_files):
            validate(inp, out, args, tagsets, known_sent_ids)
        # After reading the entire treebank (perhaps multiple files), check whether
        # the DEPS annotation was not a mere copy of the basic trees.
        if args.level>2 and line_of_first_enhanced_graph and not line_of_first_enhancement:
            testlevel = 3
            testclass = 'Enhanced'
            testid = 'edeps-identical-to-basic-trees'
            testmessage = "Enhanced graphs are copies of basic trees in the entire dataset. This can happen for some simple sentences where there is nothing to enhance, but not for all sentences. If none of the enhancements from the guidelines (https://universaldependencies.org/u/overview/enhanced-syntax.html) are annotated, the DEPS should be left unspecified"
            warn(testmessage, testclass, testlevel=testlevel, testid=testid)
    except:
        warn('Exception caught!', 'Format')
        # If the output is used in an HTML page, it must be properly escaped
        # because the traceback can contain e.g. "<module>". However, escaping
        # is beyond the goal of validation, which can be also run in a console.
        traceback.print_exc()
    if not error_counter:
        if not args.quiet:
            print('*** PASSED ***', file=sys.stderr)
        sys.exit(0)
    else:
        if not args.quiet:
            for k,v in sorted(error_counter.items()):
                print('%s errors: %d' %(k, v), file=sys.stderr)
            print('*** FAILED *** with %d errors'%sum(v for k,v in iter(error_counter.items())), file=sys.stderr)
        for f_name in sorted(warn_on_missing_files):
            filepath = os.path.join(THISDIR, 'data', f_name+'.'+args.lang)
            if not os.path.exists(filepath):
                print('The language-specific file %s does not exist.'%filepath, file=sys.stderr)
        sys.exit(1)
