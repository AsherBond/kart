#include <exception>
#include <iostream>
#include <string>
#include <memory>

#include <cppgit2/repository.hpp>

#include "kart/structure.hpp"

using namespace std;
using namespace cppgit2;
using namespace kart;

RepoStructure::RepoStructure(repository *repo, tree root_tree)
    : repo(repo), root_tree(root_tree)
{
}

vector<Dataset3 *> *RepoStructure::GetDatasets()
{
    auto result = new vector<Dataset3 *>();
    root_tree.walk(tree::traversal_mode::preorder,
                   [&](const string &parent_path, const tree::entry &entry)
                   {
                       auto type = entry.type();
                       if (type == object::object_type::tree && entry.filename() == DATASET_DIRNAME)
                       {
                           auto oid = entry.id();
                           // get parent tree; that's what the dataset uses as its root tree.
                           auto parent_entry_oid = root_tree.lookup_entry_by_path(parent_path).id();
                           auto parent_tree = repo->lookup_tree(parent_entry_oid);

                           result->push_back(new Dataset3(repo, parent_tree, parent_path));
                           return 1;
                       }
                       return 0;
                   });
    return result;
}
